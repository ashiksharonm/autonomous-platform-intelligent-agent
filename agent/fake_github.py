"""In-memory stand-ins for PyGithub and the LLM, used by tests and the offline demo.

FakeGitHub counts every REST call the agent would make so call counts can be
checked deterministically without network access. Its semantics encode the
assumption the agent was designed around: POST /issues with a label that does not
exist yet is rejected with 422. That assumption was NOT re-verified against live
GitHub (no live writes are made from this repo's tooling); GitHub may instead
auto-create the label for users with push access.
"""

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from github import GithubException


@dataclass
class FakeIssue:
    number: int
    title: str
    body: str
    labels: list
    updated_at: datetime
    state: str = "open"

    @property
    def html_url(self):
        return f"https://github.invalid/fake/repo/issues/{self.number}"


@dataclass
class FakeGitHub:
    """Fake `github.Github` client plus the single repository it serves."""

    labels: set = field(default_factory=set)
    issues: list = field(default_factory=list)
    calls: list = field(default_factory=list)
    quota: int = 5000

    def _call(self, endpoint):
        self.calls.append(endpoint)
        self.quota -= 1

    # --- github.Github surface ---------------------------------------------------
    def get_repo(self, full_name):
        self._call("GET /repos/{owner}/{repo}")
        return FakeRepo(self)

    def get_rate_limit(self):
        # Like the real endpoint, /rate_limit does not consume core quota.
        return SimpleNamespace(core=SimpleNamespace(remaining=self.quota))

    def seed_issue(self, title, labels, days_since_update):
        issue = FakeIssue(
            number=len(self.issues) + 1,
            title=title,
            body="",
            labels=[SimpleNamespace(name=name) for name in labels],
            updated_at=datetime.now(timezone.utc) - timedelta(days=days_since_update),
        )
        self.labels.update(labels)
        self.issues.append(issue)
        return issue


class FakeRepo:
    def __init__(self, gh):
        self._gh = gh

    def get_label(self, name):
        self._gh._call("GET /labels/{name}")
        if name not in self._gh.labels:
            raise GithubException(404, {"message": "Not Found"}, None)
        return SimpleNamespace(name=name)

    def create_label(self, name, color):
        self._gh._call("POST /labels")
        if name in self._gh.labels:
            raise GithubException(422, {"message": "Validation Failed", "errors": [{"code": "already_exists"}]}, None)
        self._gh.labels.add(name)
        return SimpleNamespace(name=name, color=color)

    def create_issue(self, title, body="", labels=()):
        self._gh._call("POST /issues")
        missing = [name for name in labels if name not in self._gh.labels]
        if missing:
            raise GithubException(422, {"message": "Validation Failed", "errors": [{"field": "labels", "value": missing}]}, None)
        issue = FakeIssue(
            number=len(self._gh.issues) + 1,
            title=title,
            body=body,
            labels=[SimpleNamespace(name=name) for name in labels],
            updated_at=datetime.now(timezone.utc),
        )
        self._gh.issues.append(issue)
        return issue

    def get_issues(self, state="open", labels=()):
        self._gh._call("GET /issues")
        wanted = set(labels)
        return [
            issue for issue in self._gh.issues
            if issue.state == state and wanted <= {label.name for label in issue.labels}
        ]

    def get_issue(self, number):
        self._gh._call("GET /issues/{number}")
        return self._gh.issues[number - 1]


class ScriptedPlanner:
    """Deterministic replacement for llm.chat_json.

    It is deliberately *naive*: it ignores the tool catalog, known constraints and
    memory guidance in the prompt and always emits the same plan for an instruction,
    so any improvement across runs comes from the agent's code, not from the "LLM".
    """

    def __init__(self):
        self.calls = 0

    def __call__(self, system_prompt, user_prompt, max_tries=1):
        self.calls += 1
        instruction = user_prompt.split("Instruction:", 1)[1].split("\n", 1)[0]
        if "triage summary" in instruction.lower():
            return {
                "task_type": "triage_summary_by_label_and_staleness",
                "steps": [{
                    "op_name": "NEEDS_SYNTHESIS",
                    "params": {"stale_days": 14},
                    "blocking": True,
                    "capability_request": "Group open issues by label and inactivity window and post the result as a new issue",
                    "requested_op_name": "triage_summary_by_label_and_staleness",
                }],
            }
        title = re.search(r"titled '([^']+)'", instruction).group(1)
        label = re.search(r"label '([^']+)'", instruction).group(1)
        return {
            "task_type": "create_labeled_issue",
            "steps": [{"op_name": "create_issue", "params": {"title": title, "labels": [label]}, "blocking": True}],
        }


# Hand-written stand-in for what the LLM would return for the triage-summary gap.
# It is NOT a recorded model response: no LLM API key was available.
TRIAGE_SUMMARY_SOURCE = '''
from collections import defaultdict
from datetime import datetime, timezone


def run(repo, params: dict) -> dict:
    stale_days = params.get("stale_days", 14)
    now = datetime.now(timezone.utc)
    buckets = defaultdict(lambda: {"fresh": 0, "stale": 0})
    for issue in repo.get_issues(state="open"):
        bucket = "stale" if (now - issue.updated_at).days >= stale_days else "fresh"
        for label in issue.labels or []:
            buckets[label.name][bucket] += 1
    lines = [f"- {name}: {c['fresh']} fresh, {c['stale']} stale (>= {stale_days}d)" for name, c in sorted(buckets.items())]
    summary = "Open issues by label:\\n" + "\\n".join(lines)
    actions = []
    if not params.get("dry_run"):
        issue = repo.create_issue(title="Weekly triage summary", body=summary)
        actions.append(f"created issue #{issue.number}")
    return {"summary": summary, "actions_taken": actions}
'''


class ScriptedCodeGenerator:
    """Deterministic replacement for llm.generate_code."""

    def __init__(self, source=TRIAGE_SUMMARY_SOURCE):
        self.source = source
        self.calls = 0

    def __call__(self, system_prompt, user_prompt):
        self.calls += 1
        return self.source


def dumps(obj):
    return json.dumps(obj, indent=2, sort_keys=True, default=str)
