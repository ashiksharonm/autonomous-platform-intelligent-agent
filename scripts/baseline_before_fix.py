"""Measure the ORIGINAL agent (commit 5903c51) on the same offline scenario, for a before/after.

Run from a worktree of the original commit (it has none of the fixes on this branch):

    git worktree add ../apia-before 5903c51
    cp agent/fake_github.py ../apia-before/agent/
    cp scripts/baseline_before_fix.py ../apia-before/
    cd ../apia-before && python baseline_before_fix.py > ../autonomous-platform-intelligent-agent/results/baseline_before_fix.json
"""

import json
import tempfile
from pathlib import Path

import agent.memory.db as db
from agent import core, github_tools, llm
from agent.fake_github import FakeGitHub, ScriptedPlanner

LABELS = ["needs-triage", "needs-repro", "regression", "needs-repro"]


def main():
    gh = FakeGitHub()
    llm.chat_json = ScriptedPlanner()
    db.sqlite_file_location = str(Path(tempfile.mkdtemp()) / "memory.sqlite")  # name used at 5903c51

    rows = []
    for run, label in enumerate(LABELS, start=1):
        github_tools.active_gh_session = gh
        before = len(gh.calls)
        report = core.start_agent_job(f"Create an issue titled 'Bug report {run}' with label '{label}'")
        rows.append({
            "run": run,
            "github_calls": len(gh.calls) - before,
            "agent_reported_api_calls": report.total_api_calls,
            "executed_ops": [result.step.op_name for result in report.step_results],
            "github_endpoints": gh.calls[before:],
        })
    print(json.dumps(rows, indent=1))


if __name__ == "__main__":
    main()
