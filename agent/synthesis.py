import ast
import time

from agent import llm
from agent.github_tools import fetch_my_repo
from agent.memory import capability_memory
from agent.models import StepOutcome

ALLOWED_MODULES = {"datetime", "collections", "re"}
FORBIDDEN_CALLS = {"eval", "exec", "open", "__import__"}
FORBIDDEN_MODULES = ("os", "subprocess", "sys", "shutil")

SYNTHESIS_SYSTEM_PROMPT = """You write a single Python function to fill a capability gap in an autonomous \
GitHub agent. Requirements:
- Signature MUST be exactly: def run(repo, params: dict) -> dict:
- `repo` is an already-authenticated PyGithub Repository object. Use only its methods.
- You may only use these modules (already available, do not import anything else): datetime, collections, re.
- Do not use eval, exec, open, __import__, os, subprocess, sys, or shutil.
- Any write/create call on `repo` (create_issue, create_comment, etc.) MUST be gated behind
  `if not params.get("dry_run"):`. In the dry_run branch, compute and return what WOULD be done
  without calling the write method, using real data fetched from `repo` (read calls are fine in
  dry_run mode).
- Return a dict with keys: "summary" (str) and "actions_taken" (list[str]).
- Respond with ONLY the Python code for the function, no prose, no markdown fences.
"""


class SynthesisError(Exception):
    pass


def validate_source(source):
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise SynthesisError(f"syntax error: {exc}") from exc

    run_defs = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "run"]
    if not run_defs:
        raise SynthesisError("no top-level function named 'run' found")

    arg_names = [arg.arg for arg in run_defs[0].args.args]
    if len(arg_names) < 2 or arg_names[0] != "repo" or arg_names[1] != "params":
        raise SynthesisError(f"function signature must be run(repo, params), got run({', '.join(arg_names)})")

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if isinstance(node, ast.Import):
                module = node.names[0].name.split(".")[0]
            else:
                module = node.module.split(".")[0] if node.module else ""
            if module not in ALLOWED_MODULES:
                raise SynthesisError(f"disallowed import: {module}")

        if isinstance(node, ast.Call):
            func_name = getattr(node.func, "id", None)
            if func_name in FORBIDDEN_CALLS:
                raise SynthesisError(f"disallowed call: {func_name}")

        if isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name) and node.value.id in FORBIDDEN_MODULES:
                raise SynthesisError(f"disallowed attribute access on: {node.value.id}")
            if node.attr.startswith("__") and node.attr.endswith("__"):
                raise SynthesisError(f"disallowed dunder attribute access: {node.attr}")


def load_run_function(source):
    validate_source(source)

    namespace = {}
    safe_builtins = {
        "len": len, "range": range, "sorted": sorted, "enumerate": enumerate,
        "list": list, "dict": dict, "set": set, "str": str, "int": int, "float": float,
        "bool": bool, "min": min, "max": max, "sum": sum, "isinstance": isinstance,
        "getattr": getattr, "hasattr": hasattr, "zip": zip, "reversed": reversed,
        "Exception": Exception, "ValueError": ValueError, "print": print,
    }
    exec(source, {"__builtins__": safe_builtins}, namespace)

    run = namespace.get("run")
    if run is None:
        raise SynthesisError("generated module did not define 'run'")
    return run


def dry_run_check(run, repo, params):
    try:
        dry_params = dict(params)
        dry_params["dry_run"] = True
        result = run(repo, dry_params)
    except Exception as exc:
        return False, f"dry run raised: {exc}"

    if not isinstance(result, dict) or "summary" not in result or "actions_taken" not in result:
        return False, "dry run did not return {summary, actions_taken}"
    if not result["summary"]:
        return False, "dry run summary was empty"
    return True, ""


def get_or_synthesize(op_name, description, param_schema, max_attempts=2):
    existing = capability_memory.get(op_name)
    if existing is not None and existing["source"] == "synthesized" and existing["code"]:
        return existing["code"], existing["signature_desc"]

    repo = fetch_my_repo()
    base_prompt = f"Capability needed: {description}\nExpected params shape: {param_schema or {}}"
    last_error = ""

    for attempt in range(max_attempts):
        prompt = base_prompt
        if attempt > 0:
            prompt += f"\n\nPrevious attempt failed: {last_error}\nFix it."

        source = llm.generate_code(SYNTHESIS_SYSTEM_PROMPT, prompt)
        try:
            run = load_run_function(source)
        except SynthesisError as exc:
            last_error = str(exc)
            continue

        ok, reason = dry_run_check(run, repo, {})
        if not ok:
            last_error = reason
            continue

        signature_desc = f"(synthesized) {description}"
        capability_memory.register_synthesized(op_name, source, signature_desc)
        return source, signature_desc

    raise SynthesisError(f"synthesis failed after {max_attempts} attempts: {last_error}")


def _elapsed_ms(start):
    return int((time.monotonic() - start) * 1000)


def synthesize_and_run(step):
    start = time.monotonic()
    op_name = step.requested_op_name

    if not op_name:
        return StepOutcome(
            step=step,
            status="failed",
            api_calls=0,
            latency_ms=_elapsed_ms(start),
            error_class="synthesis_failed",
            error_message="no requested_op_name provided for synthesis",
            agent_decision="decomposer did not supply a stable op_name for this capability gap; cannot register a reusable capability without one",
        )

    try:
        source, _ = get_or_synthesize(op_name, step.capability_request, step.param_schema)
    except SynthesisError as exc:
        latency_ms = _elapsed_ms(start)
        capability_memory.ensure_builtin(op_name, step.capability_request or "unknown")
        capability_memory.record_outcome(op_name, False, 0, latency_ms, "synthesis_failed")
        return StepOutcome(
            step=step,
            status="failed",
            api_calls=0,
            latency_ms=latency_ms,
            error_class="synthesis_failed",
            error_message=str(exc),
            agent_decision="could not synthesize a working implementation after retries; reporting the gap instead of guessing",
        )

    run = load_run_function(source)
    repo = fetch_my_repo()
    try:
        params = dict(step.params)
        params["dry_run"] = False
        result = run(repo, params)
    except Exception as exc:
        latency_ms = _elapsed_ms(start)
        capability_memory.record_outcome(op_name, False, 1, latency_ms, "unknown")
        return StepOutcome(
            step=step,
            status="failed",
            api_calls=1,
            latency_ms=latency_ms,
            error_class="unknown",
            error_message=str(exc)[:500],
            agent_decision="synthesized function raised on real execution; step marked failed",
        )

    latency_ms = _elapsed_ms(start)
    capability_memory.record_outcome(op_name, True, 1, latency_ms)
    return StepOutcome(step=step, status="ok", api_calls=1, latency_ms=latency_ms, artifacts=result)
