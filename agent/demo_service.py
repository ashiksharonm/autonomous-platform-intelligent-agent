"""UI-independent logic behind the hosted demo: plan -> (approve | reject) -> execute.

State is per process: one in-memory fake GitHub repo, one memory DB and one audit log in a temp
directory. `reset()` wipes all three. Nothing here can reach real GitHub.
"""

import tempfile
import threading
from pathlib import Path

from agent import config, core, github_tools
from agent.memory import db
from agent.policy import AuditLog, PolicyError, parse_policy, verify_audit_log

SAMPLES = [
    ("Allowed: create a labeled issue", "Create an issue titled 'Fix login bug' with label 'bug'"),
    ("Needs approval: comment on an issue", "Comment on issue #1 saying 'Looking into this'"),
    ("Denied: blast radius (6 writes > 4)", "Create 6 issues titled 'Spam'"),
    ("Denied: op not in allowed_ops", "Close all issues in the repo"),
    ("Needs approval: LLM-written code", "Generate this week's triage summary"),
    ("Allowed: read-only search", "Find stale issues labeled 'bug'"),
]


class DemoService:
    def __init__(self, policy_path="policies/default.yaml"):
        self.default_policy = Path(policy_path).read_text()
        self.lock = threading.RLock()
        self.pending = {}
        self.reset()

    def reset(self):
        with self.lock:
            self._tmp = tempfile.TemporaryDirectory(prefix="agent-demo-")
            root = Path(self._tmp.name)
            db.DB_PATH = str(root / "memory.sqlite")
            config.GITHUB_MODE = "mock"
            github_tools.reset_session()
            self.audit = AuditLog(root / "audit.jsonl")
            self.pending = {}

    def submit(self, instruction, policy_text, dry_run=False):
        """Returns {status, plan, report?, request_id?}; status is one of core's outcomes or awaiting_approval."""
        if not instruction.strip():
            return {"status": "error", "plan": "Enter an instruction first."}
        try:
            policy = parse_policy(policy_text, source="ui-policy")
        except PolicyError as exc:
            return {"status": "error", "plan": f"Policy error: {exc}"}
        with self.lock:
            job = core.plan_job(instruction, policy=policy, audit=self.audit)
            action = core.gate(job, dry_run=dry_run, audit=self.audit)
            result = {"request_id": job.request_id, "plan": job.plan_text}
            if action in (core.BLOCKED, core.DRY_RUN):
                return {**result, "status": action}
            if action == core.AWAITING_APPROVAL:
                self.pending[job.request_id] = job
                return {**result, "status": core.AWAITING_APPROVAL}
            return {**result, "status": "executed", "report": core.execute_job(job, audit=self.audit).to_text()}

    def decide(self, request_id, approve):
        with self.lock:
            job = self.pending.pop(request_id, None)
            if job is None:
                return {"status": "error", "plan": "No pending request (already decided, or the demo was reset)."}
            who = core.record_approval(job, approve, "demo-ui", self.audit)
            if not approve:
                return {"status": core.REJECTED, "request_id": request_id, "plan": job.plan_text}
            report = core.execute_job(job, audit=self.audit, approved_by=who)
            return {"status": "executed", "request_id": request_id, "plan": job.plan_text, "report": report.to_text()}

    def audit_text(self):
        records = self.audit.read()
        ok, message = verify_audit_log(self.audit.path) if records else (True, "empty")
        lines = [f"[{'chain OK' if ok else 'CHAIN BROKEN'}] {message}", ""]
        lines += [
            f"#{r['seq']} {r['request_id']} {r['event']:<9} "
            + ", ".join(f"{k}={v}" for k, v in r.items() if k in ("task_type", "outcome", "approved", "by", "op", "status", "reasons"))
            for r in records[-40:]
        ]
        return "\n".join(lines)

    def repo_text(self):
        gh = github_tools.create_gh_session()
        rows = [f"#{i.number} [{', '.join(label.name for label in i.labels) or '-'}] {i.title}" for i in gh.issues]
        return f"{config.effective_repo()} (in-memory fake; {len(gh.calls)} API calls so far)\n" + "\n".join(rows)
