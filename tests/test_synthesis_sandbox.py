import pytest

from agent.sandbox import SandboxError, load_run_function, run_in_subprocess, validate_source

GOOD_SOURCE = '''
import re
from collections import Counter
from datetime import datetime, timezone

def run(repo, params: dict) -> dict:
    words = re.findall(r"[a-z]+", params.get("text", ""))
    counts = Counter(words)
    stamp = datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat()
    top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:2]
    return {"summary": f"{top} @ {stamp}", "actions_taken": []}
'''


def test_good_code_passes_and_runs_with_allowed_imports():
    # Regression: allowed imports used to raise ImportError ("__import__ not found").
    run = load_run_function(GOOD_SOURCE)
    result = run(None, {"text": "b a b c"})
    assert result["summary"] == "[('b', 2), ('a', 1)] @ 2026-01-01T00:00:00+00:00"


def test_top_level_import_visible_inside_run():
    # Regression: exec used separate globals/locals, so a module-level import was not
    # visible from inside run().
    source = "import datetime\ndef run(repo, params):\n    return {'summary': str(datetime.date(2026, 1, 2)), 'actions_taken': []}\n"
    assert load_run_function(source)(None, {})["summary"] == "2026-01-02"


def test_allowed_module_is_preinjected_without_import():
    source = "def run(repo, params):\n    return {'summary': re.sub('a', 'b', 'aa'), 'actions_taken': []}\n"
    assert load_run_function(source)(None, {})["summary"] == "bb"


@pytest.mark.parametrize(
    "source",
    [
        "def run(first_arg, second_arg):\n    return {}\n",
        "def helper(repo, params):\n    return {}\n",
        "def run(repo, params:\n    pass",
    ],
)
def test_malformed_sources_rejected(source):
    with pytest.raises(SandboxError):
        validate_source(source)


@pytest.mark.parametrize(
    "header",
    [
        "import os",
        "import re, os",  # regression: only names[0] used to be checked
        "import datetime, subprocess",
        "import collections.abc",
        "from os import path",
        "from re import _compiler",
        "from . import config",
        "from datetime import *",
    ],
)
def test_disallowed_imports_rejected(header):
    with pytest.raises(SandboxError):
        validate_source(header + "\ndef run(repo, params):\n    return {}\n")


ESCAPE_BODIES = {
    "getattr_chain": 'return getattr(getattr(params, "__class__"), "__mro__")[1].__subclasses__()',
    "getattr_split_string": 'return getattr(getattr(params, "__cl" + "ass__"), "__base__")',
    "getattr_concat_no_dunder_literal": 'n = "_" + "_class_" + "_"\n    return getattr(params, n)',
    "dunder_attribute": "return repo.__class__.__bases__",
    "private_attribute": "return repo._requester",
    "format_string_attr": 'return "{0.__class__}".format(params)',
    "format_string_private": 'return "{0._requester}".format(repo)',
    "format_map": 'return "{x}".format_map(params)',
    "lambda_default_globals": "f = (lambda x=0: x)\n    return f.__globals__",
    "generator_frame": "g = (i for i in [1])\n    return g.gi_frame.f_back.f_globals",
    "fstring_dunder": 'return f"{params.__class__}"',
    "fstring_frame": 'return f"{(i for i in []).gi_frame}"',
    "subscript_dunder_key": 'return {"a": 1}["__builtins__"]',
    "builtins_name": "return __builtins__",
    "dunder_import_call": '__import__("os").system("true")',
    "eval_call": 'return eval("1+1")',
    "exec_call": 'exec("x=1")',
    "open_call": 'return open("/etc/passwd").read()',
    "type_call": "return type(params)",
    "vars_call": "return vars(params)",
    "os_name": 'os.system("true")',
    "mro_attribute": "return dict.mro()",
    "traceback_walk": "try:\n        1/0\n    except Exception as e:\n        return e.__traceback__.tb_frame",
    "with_traceback": "try:\n        1/0\n    except Exception as e:\n        return e.with_traceback(None)",
}


@pytest.mark.parametrize("name", sorted(ESCAPE_BODIES))
def test_escape_payloads_rejected(name):
    source = "def run(repo, params):\n    " + ESCAPE_BODIES[name] + "\n"
    with pytest.raises(SandboxError):
        validate_source(source)


@pytest.mark.parametrize(
    "source",
    [
        "class X:\n    pass\ndef run(repo, params):\n    return {}\n",
        "def run(repo, params):\n    with x:\n        pass\n",
        "def run(repo, params):\n    global y\n    return {}\n",
        "def run(repo, params):\n    yield 1\n",
        "async def run(repo, params):\n    return {}\n",
        "x = run(None, {})\ndef run(repo, params):\n    return {}\n",
        "while True:\n    pass\ndef run(repo, params):\n    return {}\n",
    ],
)
def test_disallowed_syntax_rejected(source):
    with pytest.raises(SandboxError):
        validate_source(source)


def test_original_getattr_escape_no_longer_runs():
    # The exact payload that reached object.__subclasses__() before the fix.
    source = (
        "def run(repo, params):\n"
        '    obj = getattr(getattr(params, "__cl" + "ass__"), "__m" + "ro__")[1]\n'
        '    return {"summary": str(len(getattr(obj, "__subcl" + "asses__")())), "actions_taken": []}\n'
    )
    with pytest.raises(SandboxError):
        load_run_function(source)


def test_runtime_builtins_are_restricted():
    # Even if validation were bypassed, the exec namespace lacks dangerous builtins.
    from agent.sandbox import build_namespace

    builtins = build_namespace()["__builtins__"]
    for name in ("getattr", "setattr", "hasattr", "vars", "type", "eval", "exec", "open", "compile", "globals"):
        assert name not in builtins
    with pytest.raises(ImportError):
        builtins["__import__"]("os")
    re_view = builtins["__import__"]("re")
    assert not hasattr(re_view, "enum") and not hasattr(re_view, "functools")
    assert not hasattr(builtins["__import__"]("datetime"), "sys")


@pytest.mark.parametrize("header", ["from re import enum", "from datetime import sys"])
def test_submodules_of_allowed_modules_not_importable(header):
    # These pass the AST check but the module views strip submodules at runtime.
    with pytest.raises(SandboxError, match="ImportError"):
        load_run_function(header + "\ndef run(repo, params):\n    return {}\n")


def test_submodule_walk_blocked_at_runtime():
    run = load_run_function('def run(repo, params):\n    return re.enum.sys.modules["os"]\n')
    with pytest.raises(AttributeError):
        run(None, {})


def test_subprocess_runner_returns_result():
    result = run_in_subprocess(GOOD_SOURCE, {"text": "x y x"}, repo_mode="none", timeout=20)
    assert result["summary"].startswith("[('x', 2), ('y', 1)]")


def test_subprocess_runner_enforces_timeout():
    source = "def run(repo, params):\n    while True:\n        pass\n"
    with pytest.raises(SandboxError, match="timeout"):
        run_in_subprocess(source, {}, repo_mode="none", timeout=2)


def test_subprocess_runner_reports_errors():
    source = "def run(repo, params):\n    raise ValueError('boom')\n"
    with pytest.raises(SandboxError, match="ValueError: boom"):
        run_in_subprocess(source, {}, repo_mode="none", timeout=20)
