import time
import uuid

from github import GithubException

from agent import github_tools
from agent.github_tools import TOOL_REGISTRY
from agent.memory import capability_memory, execution_memory
from agent.models import JobStep, StepOutcome

# Constraint recorded on create_issue when a 422 is recovered by creating the label.
LABEL_MUST_EXIST = "label must exist before use in create_issue call"

ERROR_DECISIONS = {
    "422_validation": "step failed validation; will attempt a targeted fix (e.g. create missing label) and retry once before giving up on this step",
    "404_not_found": "referenced resource does not exist; skipping this step rather than retrying",
    "403_permission": "insufficient permissions for this operation; aborting run rather than silently skipping a write the user asked for",
    "rate_limited": "hit a GitHub rate limit; recording constraint for future runs and aborting this step rather than retrying blindly",
    "synthesis_failed": "could not synthesize a working implementation after retries; reporting the gap instead of guessing",
    "unknown": "unexpected error; step marked failed, no automatic retry attempted",
}


def classify_error(exc):
    if isinstance(exc, GithubException):
        return {
            422: "422_validation",
            404: "404_not_found",
            403: "403_permission",
            429: "rate_limited",
        }.get(exc.status, "unknown")
    return "unknown"


def _elapsed_ms(start):
    return int((time.monotonic() - start) * 1000)


def apply_memory_guard(task_type, steps):
    """Rewrite the LLM's plan using stored history, independent of what the LLM decided.

    1. If the plan uses an op that has failed >50% of the time for this task_type and a
       known-good decomposition exists, swap in the known-good sequence.
    2. If create_issue has the learned "label must exist" constraint, insert an
       ensure_label_exists step before any create_issue that uses a label not already
       ensured earlier in the plan.
    """
    guidance = execution_memory.derive_guidance(task_type)
    planned_ops = {step.op_name for step in steps}
    if guidance["preferred_decomposition"] and planned_ops & set(guidance["avoid_ops"]):
        steps = [
            JobStep(op_name=p["op_name"], params=p.get("params", {}), blocking=p.get("blocking", True))
            for p in guidance["preferred_decomposition"]
        ]
    return apply_learned_constraints(steps)


def apply_learned_constraints(steps):
    create_issue = capability_memory.get("create_issue")
    learned = {c["detail"] for c in create_issue["known_constraints"]} if create_issue else set()
    if LABEL_MUST_EXIST not in learned:
        return steps

    ensured = set()
    guarded = []
    for step in steps:
        if step.op_name == "ensure_label_exists":
            ensured.add(step.params.get("name"))
        elif step.op_name == "create_issue":
            for label in step.params.get("labels") or []:
                if label not in ensured:
                    guarded.append(JobStep(op_name="ensure_label_exists", params={"name": label}, blocking=True))
                    ensured.add(label)
        guarded.append(step)
    return guarded


def run_single_step(step):
    """Execute one step (built-in or synthesized), tracking API calls and latency."""
    from agent import synthesis

    start = time.monotonic()
    quota_start = github_tools.quota_snapshot()

    if step.op_name == "NEEDS_SYNTHESIS":
        outcome = synthesis.synthesize_and_run(step)
        if not outcome.latency_ms:
            outcome.latency_ms = _elapsed_ms(start)
        return outcome

    builtin = TOOL_REGISTRY.get(step.op_name)
    stored = capability_memory.get(step.op_name)

    try:
        if builtin is not None:
            artifacts = builtin["fn"](github_tools.fetch_my_repo(), step.params)
        elif stored is not None and stored["source"] == "synthesized":
            result = synthesis.execute_capability(stored["code"], step.params)
            artifacts = result if isinstance(result, dict) else {"result": result}
        else:
            raise RuntimeError(f"No tool or synthesized capability registered for '{step.op_name}'")
    except Exception as exc:
        latency_ms = _elapsed_ms(start)
        api_calls = github_tools.calls_used_since(quota_start)
        error_class = classify_error(exc)
        decision = ERROR_DECISIONS.get(error_class, ERROR_DECISIONS["unknown"])
        capability_memory.record_outcome(step.op_name, False, api_calls, latency_ms, error_class)

        if error_class == "422_validation" and step.op_name == "create_issue" and "labels" in step.params:
            recovered = recover_missing_labels(step)
            if recovered is not None:
                # Count the failed first attempt too, so the run total reflects every call made.
                recovered.api_calls += api_calls
                recovered.latency_ms += latency_ms
                recovered.agent_decision = decision + " (recovered: created missing label then retried)"
                return recovered

        capability_memory.add_constraint(step.op_name, error_class, str(exc)[:200])
        return StepOutcome(
            step=step,
            status="failed",
            api_calls=api_calls,
            latency_ms=latency_ms,
            error_class=error_class,
            error_message=str(exc)[:500],
            agent_decision=decision,
        )

    latency_ms = _elapsed_ms(start)
    api_calls = github_tools.calls_used_since(quota_start)
    capability_memory.record_outcome(step.op_name, True, api_calls, latency_ms)
    return StepOutcome(step=step, status="ok", api_calls=api_calls, latency_ms=latency_ms, artifacts=artifacts)


def recover_missing_labels(step):
    """After a 422 on create_issue: create any missing labels, then retry once."""
    start = time.monotonic()
    quota_start = github_tools.quota_snapshot()
    repo = github_tools.fetch_my_repo()
    labels = step.params.get("labels", [])

    try:
        for label in labels:
            try:
                repo.get_label(label)
            except GithubException as exc:
                if exc.status != 404:
                    raise
                repo.create_label(name=label, color="ededed")
                capability_memory.add_constraint("create_issue", "422_validation", LABEL_MUST_EXIST)

        issue = repo.create_issue(title=step.params["title"], body=step.params.get("body", ""), labels=labels)
    except Exception:
        return None

    latency_ms = _elapsed_ms(start)
    api_calls = github_tools.calls_used_since(quota_start)
    capability_memory.record_outcome("create_issue", True, api_calls, latency_ms)
    return StepOutcome(
        step=step,
        status="ok",
        api_calls=api_calls,
        latency_ms=latency_ms,
        artifacts={"issue_url": issue.html_url, "issue_number": issue.number},
    )


def new_step_id():
    return str(uuid.uuid4())
