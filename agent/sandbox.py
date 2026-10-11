"""Validation and restricted execution for LLM-synthesized capability code.

Threat model (read this before trusting it):

* The code being run is written by an LLM on behalf of the same user whose GitHub
  token the agent already holds. The goal is to stop *accidental* or prompt-injected
  misuse (shelling out, reading files, walking Python internals to reach `os`), not to
  contain a determined attacker.
* In-process Python sandboxing is NOT a security boundary. CPython has a long history of
  escapes from restricted-exec environments, and the `repo` object handed to the code is
  a live, authenticated PyGithub client. Defence here is layered and best-effort:
    1. `validate_source` - an AST *allowlist*: only listed node types, no dunder or
       private (`_x`) names/attributes/strings, no frame/generator/traceback
       introspection attributes, no `str.format`, imports limited to an allowlist and
       checked for every alias.
    2. `build_namespace` - exec with a tiny builtins dict (no getattr/type/vars/...) and a
       restricted `__import__` that only hands out read-only views of allowlisted
       modules with their submodules stripped (e.g. `re.enum.sys` is unreachable).
    3. `agent.sandbox_runner` - by default the code runs in a separate Python process
       with a wall-clock timeout, CPU/file-size/fd/process rlimits and a scrubbed
       environment (only the GitHub token and repo name are passed through).
* For anything beyond "one authenticated user's own scratch repo", run the agent (or at
  least the runner) inside a container/VM with no host filesystem and egress limited to
  api.github.com.
"""

import ast
import collections
import datetime
import json
import os
import re
import subprocess
import sys
import types
from pathlib import Path

ALLOWED_MODULES = {"collections": collections, "datetime": datetime, "re": re}

# Statement/expression node types synthesized code may contain. Anything else
# (classes, async, with, global/nonlocal, yield, match, ...) is rejected.
ALLOWED_NODES = (
    ast.Module, ast.FunctionDef, ast.Lambda, ast.arguments, ast.arg,
    ast.Return, ast.Assign, ast.AugAssign, ast.AnnAssign, ast.Delete,
    ast.For, ast.While, ast.If, ast.Break, ast.Continue, ast.Pass, ast.Expr,
    ast.Try, ast.ExceptHandler, ast.Raise, ast.Assert,
    ast.Import, ast.ImportFrom, ast.alias,
    ast.BoolOp, ast.BinOp, ast.UnaryOp, ast.Compare, ast.Call, ast.keyword, ast.IfExp,
    ast.Attribute, ast.Subscript, ast.Slice, ast.Starred, ast.Name, ast.Constant,
    ast.List, ast.Tuple, ast.Dict, ast.Set,
    ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp, ast.comprehension,
    ast.JoinedStr, ast.FormattedValue, ast.NamedExpr,
    ast.expr_context, ast.operator, ast.boolop, ast.cmpop, ast.unaryop,
)

TOP_LEVEL_STATEMENTS = (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.Assign, ast.AnnAssign, ast.Expr)

FORBIDDEN_NAMES = {
    "eval", "exec", "compile", "open", "input", "breakpoint", "help", "exit", "quit",
    "globals", "locals", "vars", "dir", "getattr", "setattr", "delattr", "hasattr",
    "type", "object", "super", "memoryview", "classmethod", "staticmethod", "property",
    "os", "sys", "subprocess", "shutil", "socket", "builtins", "importlib", "ctypes", "pickle",
}

# Attributes that expose interpreter internals without using dunder names, plus
# str.format/format_map whose field syntax ("{0.attr}") performs attribute lookups
# the AST cannot see.
FORBIDDEN_ATTRIBUTES = {
    "format", "format_map", "mro",
    "gi_frame", "gi_code", "gi_yieldfrom", "gi_running", "gi_suspended",
    "cr_frame", "cr_code", "cr_await", "ag_frame", "ag_code", "ag_await",
    "f_back", "f_globals", "f_locals", "f_builtins", "f_code", "f_trace",
    "tb_frame", "tb_next", "co_code", "co_consts", "func_globals", "func_code",
    "with_traceback",
}

SAFE_BUILTINS = {
    "abs": abs, "all": all, "any": any, "bool": bool, "dict": dict, "enumerate": enumerate,
    "filter": filter, "float": float, "frozenset": frozenset, "int": int, "isinstance": isinstance,
    "iter": iter, "len": len, "list": list, "map": map, "max": max, "min": min, "next": next,
    "range": range, "repr": repr, "reversed": reversed, "round": round, "set": set,
    "sorted": sorted, "str": str, "sum": sum, "tuple": tuple, "zip": zip,
    "Exception": Exception, "ValueError": ValueError, "KeyError": KeyError, "TypeError": TypeError,
    "IndexError": IndexError, "AttributeError": AttributeError, "RuntimeError": RuntimeError,
    "StopIteration": StopIteration,
}

RUNNER_TIMEOUT_S = float(os.environ.get("AGENT_SANDBOX_TIMEOUT_S", "30"))
PROJECT_ROOT = Path(__file__).resolve().parent.parent


class SandboxError(Exception):
    """Source was rejected, failed to load, or failed inside the sandbox."""


def _is_private(name):
    return name.startswith("_")


def validate_source(source):
    """Reject anything outside the allowlisted subset. Returns the parsed tree."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise SandboxError(f"syntax error: {exc}") from exc

    run_defs = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "run"]
    if not run_defs:
        raise SandboxError("no top-level function named 'run' found")
    arg_names = [arg.arg for arg in run_defs[0].args.args]
    if len(arg_names) < 2 or arg_names[:2] != ["repo", "params"]:
        raise SandboxError(f"function signature must be run(repo, params), got run({', '.join(arg_names)})")

    for stmt in tree.body:
        if not isinstance(stmt, TOP_LEVEL_STATEMENTS):
            raise SandboxError(f"disallowed top-level statement: {type(stmt).__name__}")
        if isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.Expr)) and any(
            isinstance(node, (ast.Call, ast.Lambda)) for node in ast.walk(stmt)
        ):
            raise SandboxError("top-level code may only define constants, imports and functions")

    for node in ast.walk(tree):
        if not isinstance(node, ALLOWED_NODES):
            raise SandboxError(f"disallowed syntax: {type(node).__name__}")

        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name not in ALLOWED_MODULES:
                    raise SandboxError(f"disallowed import: {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.level or node.module not in ALLOWED_MODULES:
                raise SandboxError(f"disallowed import: {'.' * node.level}{node.module or ''}")
            for alias in node.names:
                if alias.name == "*" or _is_private(alias.name):
                    raise SandboxError(f"disallowed import name: {alias.name}")
        elif isinstance(node, ast.Name):
            if node.id.startswith("__") or node.id in FORBIDDEN_NAMES:
                raise SandboxError(f"disallowed name: {node.id}")
        elif isinstance(node, ast.Attribute):
            if _is_private(node.attr) or node.attr in FORBIDDEN_ATTRIBUTES:
                raise SandboxError(f"disallowed attribute access: {node.attr}")
        elif isinstance(node, ast.Constant):
            if isinstance(node.value, (str, bytes)) and ("__" in str(node.value)):
                raise SandboxError("string constants may not contain '__'")
        elif isinstance(node, (ast.FunctionDef, ast.arg)):
            name = node.name if isinstance(node, ast.FunctionDef) else node.arg
            if name.startswith("__"):
                raise SandboxError(f"disallowed identifier: {name}")
        elif isinstance(node, ast.alias) and node.asname and node.asname.startswith("__"):
            raise SandboxError(f"disallowed identifier: {node.asname}")
    return tree


def _module_view(module):
    """Read-only-ish namespace of a module's public, non-module attributes."""
    attrs = {}
    for name in dir(module):
        if _is_private(name):
            continue
        value = getattr(module, name)
        if isinstance(value, types.ModuleType):
            continue
        attrs[name] = value
    return types.SimpleNamespace(**attrs)


def build_namespace():
    views = {name: _module_view(module) for name, module in ALLOWED_MODULES.items()}

    def restricted_import(name, globals=None, locals=None, fromlist=(), level=0):
        if level != 0 or name not in views:
            raise ImportError(f"import of '{name}' is not allowed in synthesized code")
        return views[name]

    builtins = dict(SAFE_BUILTINS)
    builtins["__import__"] = restricted_import
    # The allowed modules are also pre-injected so code that skips the import still works.
    namespace = {"__builtins__": builtins, "__name__": "synthesized_capability"}
    namespace.update(views)
    return namespace


def load_run_function(source):
    """Validate and exec `source` in the restricted namespace; return its `run`."""
    validate_source(source)
    namespace = build_namespace()
    try:
        exec(compile(source, "<synthesized>", "exec"), namespace)  # noqa: S102 - validated above
    except Exception as exc:
        raise SandboxError(f"loading synthesized code failed: {type(exc).__name__}: {exc}") from exc
    run = namespace.get("run")
    if not callable(run):
        raise SandboxError("generated module did not define 'run'")
    return run


def run_in_subprocess(source, params, *, repo_mode="github", timeout=None):
    """Run `source`'s run(repo, params) in a separate, resource-limited process.

    repo_mode="github" builds a PyGithub repo from GITHUB_TOKEN/GITHUB_REPO inside the
    child; repo_mode="none" passes repo=None (used by tests).
    """
    validate_source(source)  # fail fast in the parent; the child validates again
    timeout = RUNNER_TIMEOUT_S if timeout is None else timeout
    env = {"PATH": os.environ.get("PATH", ""), "AGENT_SANDBOX_TIMEOUT_S": str(timeout)}
    for key in ("GITHUB_TOKEN", "GITHUB_REPO"):
        if key in os.environ:
            env[key] = os.environ[key]
    if repo_mode == "github":
        from agent import config

        env.setdefault("GITHUB_TOKEN", config.GITHUB_TOKEN)
        env.setdefault("GITHUB_REPO", config.GITHUB_REPO)

    payload = json.dumps({"source": source, "params": params, "repo_mode": repo_mode})
    try:
        proc = subprocess.run(
            [sys.executable, "-E", "-s", "-B", "-m", "agent.sandbox_runner"],
            input=payload, capture_output=True, text=True, timeout=timeout,
            cwd=PROJECT_ROOT, env=env, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise SandboxError(f"synthesized code exceeded the {timeout:g}s timeout") from exc

    try:
        reply = json.loads(proc.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        detail = (proc.stderr or "").strip()[-300:]
        raise SandboxError(f"sandbox process exited with code {proc.returncode}: {detail}") from exc
    if not reply.get("ok"):
        raise SandboxError(reply.get("error", "unknown sandbox error"))
    return reply["result"]
