import time

from agent import config, github_tools, llm, sandbox
from agent.memory import capability_memory
from agent.models import StepOutcome
from agent.sandbox import SandboxError

SYNTHESIS_SYSTEM_PROMPT = """You write a single Python function to fill a capability gap in an autonomous \
GitHub agent. Requirements:
- Signature MUST be exactly: def run(repo, params: dict) -> dict:
- `repo` is an already-authenticated PyGithub Repository object. Use only its methods.
- You may only use these modules: datetime, collections, re (import them normally, e.g. `import re`).
- Do not use eval, exec, open, getattr, type, os, subprocess, sys, or shutil; do not use str.format
  (use f-strings), classes, `with`, generators/yield, or any name/attribute/string containing `__`
  or starting with `_`.
- Any write/create call on `repo` (create_issue, create_comment, etc.) MUST be gated behind
  `if not params.get("dry_run"):`. In the dry_run branch, compute and return what WOULD be done
  without calling the write method, using real data fetched from `repo` (read calls are fine in
  dry_run mode).
- Return a dict with keys: "summary" (str) and "actions_taken" (list[str]).
- Respond with ONLY the Python code for the function, no prose, no markdown fences.
"""


class SynthesisError(Exception):
    pass


def execute_capability(source, params):
    """Run synthesized `source` against the configured repo, honouring SANDBOX_MODE."""
    # Mock GitHub objects live in this process, so the subprocess runner cannot reach them.
    if config.SANDBOX_MODE == "inprocess" or config.GITHUB_MODE == "mock":
        return sandbox.load_run_function(source)(github_tools.fetch_my_repo(), params)
    return sandbox.run_in_subprocess(source, params)


def dry_run_check(source, params):
    dry_params = dict(params)
    dry_params["dry_run"] = True
    try:
        result = execute_capability(source, dry_params)
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

    base_prompt = f"Capability needed: {description}\nExpected params shape: {param_schema or {}}"
    last_error = ""

    for attempt in range(max_attempts):
        prompt = base_prompt
        if attempt > 0:
            prompt += f"\n\nPrevious attempt failed: {last_error}\nFix it."

        source = llm.generate_code(SYNTHESIS_SYSTEM_PROMPT, prompt)
        try:
            sandbox.validate_source(source)
        except SandboxError as exc:
            last_error = str(exc)
            continue

        ok, reason = dry_run_check(source, {})
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
    quota_start = github_tools.quota_snapshot()  # includes dry-run calls made during synthesis
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
        api_calls = github_tools.calls_used_since(quota_start)
        capability_memory.ensure_builtin(op_name, step.capability_request or "unknown")
        capability_memory.record_outcome(op_name, False, api_calls, latency_ms, "synthesis_failed")
        return StepOutcome(
            step=step,
            status="failed",
            api_calls=api_calls,
            latency_ms=latency_ms,
            error_class="synthesis_failed",
            error_message=str(exc),
            agent_decision="could not synthesize a working implementation after retries; reporting the gap instead of guessing",
        )

    try:
        params = dict(step.params)
        params["dry_run"] = False
        result = execute_capability(source, params)
    except Exception as exc:
        latency_ms = _elapsed_ms(start)
        api_calls = github_tools.calls_used_since(quota_start)
        capability_memory.record_outcome(op_name, False, api_calls, latency_ms, "unknown")
        return StepOutcome(
            step=step,
            status="failed",
            api_calls=api_calls,
            latency_ms=latency_ms,
            error_class="unknown",
            error_message=str(exc)[:500],
            agent_decision="synthesized function raised on real execution; step marked failed",
        )

    latency_ms = _elapsed_ms(start)
    api_calls = github_tools.calls_used_since(quota_start)
    capability_memory.record_outcome(op_name, True, api_calls, latency_ms)
    return StepOutcome(step=step, status="ok", api_calls=api_calls, latency_ms=latency_ms, artifacts=result)
