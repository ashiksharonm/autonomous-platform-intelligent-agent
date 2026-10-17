"""Deterministic keyword planner used when no LLM key is configured (LLM_PROVIDER=mock).

This is NOT a language model. It recognises a handful of instruction shapes with regexes and
emits the same JSON a model would, so the policy gate, dry-run, approval and audit log can be
demonstrated and tested for free. Unrecognised instructions plan zero steps.
"""

import re

from agent.fake_github import TRIAGE_SUMMARY_SOURCE

_Q = r"""['"]([^'"]+)['"]"""


def _step(op, params, blocking=True):
    return {"op_name": op, "params": params, "blocking": blocking, "capability_request": None}


def plan_instruction(text: str) -> dict:
    low = text.lower()
    m = re.search(rf"create (\d+) issues? titled {_Q}", text, re.I)
    if m and int(m.group(1)) > 1:
        n = int(m.group(1))
        steps = [_step("create_issue", {"title": f"{m.group(2)} {i}"}, blocking=False) for i in range(1, n + 1)]
        return {"task_type": "bulk_create_issues", "steps": steps}
    m = re.search(rf"create (?:an? )?issue titled {_Q}(?: with labels? {_Q})?", text, re.I)
    if m:
        params = {"title": m.group(1)}
        if m.group(2):
            params["labels"] = [m.group(2)]
        return {"task_type": "create_labeled_issue" if m.group(2) else "create_issue", "steps": [_step("create_issue", params)]}
    m = re.search(rf"comment on issue #?(\d+)\D+{_Q}", text, re.I)
    if m:
        return {"task_type": "comment_on_issue", "steps": [_step("comment_on_issue", {"issue_number": int(m.group(1)), "body": m.group(2)})]}
    m = re.search(rf"add label {_Q} to issue #?(\d+)", text, re.I)
    if m:
        params = {"issue_number": int(m.group(2)), "labels": [m.group(1)]}
        return {"task_type": "label_issue", "steps": [_step("ensure_label_exists", {"name": m.group(1)}), _step("add_labels_to_issue", params)]}
    m = re.search(rf"stale issues? (?:labeled|with label) {_Q}", text, re.I)
    if m:
        return {"task_type": "stale_issue_triage", "steps": [_step("find_stale_labeled_issues", {"label": m.group(1)})]}
    if "triage summary" in low:
        return {
            "task_type": "triage_summary_by_label_and_staleness",
            "steps": [
                {
                    "op_name": "NEEDS_SYNTHESIS",
                    "params": {"stale_days": 14},
                    "blocking": True,
                    "capability_request": "Group open issues by label and inactivity window and post the result as a new issue",
                    "requested_op_name": "triage_summary_by_label_and_staleness",
                }
            ],
        }
    if re.search(r"\b(delete|close) (all|every)\b", low):
        # A destructive request the catalog cannot do: planned as an unknown op so the policy gate refuses it.
        return {"task_type": "bulk_destructive", "steps": [_step("close_all_issues", {})]}
    return {"task_type": "unrecognised", "steps": []}


def plan(system_prompt, user_prompt, max_tries=1):
    instruction = user_prompt.split("Instruction:", 1)[1].split("\n", 1)[0].strip()
    return plan_instruction(instruction)


def generate_code(system_prompt, user_prompt):
    return TRIAGE_SUMMARY_SOURCE
