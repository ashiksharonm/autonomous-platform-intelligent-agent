import json
from pathlib import Path

import pytest

import agent.memory.db as db_module
from agent import executor
from agent.memory import capability_memory
from agent.models import JobStep
from scripts.offline_learning_demo import labeled_issue_instructions, run_scenario

RESULTS = Path(__file__).resolve().parent.parent / "results" / "offline_learning_demo.json"


@pytest.fixture
def memory_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", str(tmp_path / "test.sqlite"))
    db_module.setup_tables()
    capability_memory.ensure_builtin("create_issue", "Create an issue")


def test_recovered_step_counts_failed_attempt(tmp_path):
    # Regression: the recovered StepOutcome used to drop the failed first POST /issues
    # (and its GET /repos), under-reporting run 1 as 3 calls when 6 were made.
    rows, _ = run_scenario(labeled_issue_instructions()[:1], workdir=tmp_path)
    assert rows[0]["github_endpoints"] == [
        "GET /repos/{owner}/{repo}", "POST /issues", "GET /labels/{name}", "POST /labels", "POST /issues",
    ]
    assert rows[0]["agent_reported_api_calls"] == rows[0]["github_calls"] == 5


def test_learned_label_constraint_avoids_422_on_new_label(tmp_path):
    rows, _ = run_scenario(labeled_issue_instructions()[:2], workdir=tmp_path)
    run2 = rows[1]
    assert run2["executed_ops"] == ["ensure_label_exists", "create_issue"]
    assert run2["github_endpoints"].count("POST /issues") == 1  # no failed attempt
    assert run2["github_calls"] == 4 < rows[0]["github_calls"]


def test_without_memory_every_new_label_pays_the_422(tmp_path):
    rows, _ = run_scenario(labeled_issue_instructions()[:3], keep_memory=False, workdir=tmp_path)
    assert [row["github_calls"] for row in rows] == [5, 5, 5]


def test_synthesized_capability_is_reused(tmp_path):
    from agent.fake_github import FakeGitHub

    gh = FakeGitHub()
    gh.seed_issue("Login fails", ["bug"], days_since_update=30)
    rows, _ = run_scenario(["Generate this week's triage summary"] * 2, gh=gh, workdir=tmp_path)
    assert [row["outcome"] for row in rows] == ["success", "success"]
    assert [row["llm_codegen_calls"] for row in rows] == [1, 0]
    assert rows[1]["github_calls"] < rows[0]["github_calls"]  # no dry run on reuse
    assert "1 stale" in gh.issues[-1].body


def test_guard_is_noop_without_constraint(memory_db):
    steps = [JobStep(op_name="create_issue", params={"title": "t", "labels": ["x"]})]
    assert executor.apply_learned_constraints(steps) == steps


def test_guard_does_not_duplicate_existing_ensure_step(memory_db):
    capability_memory.add_constraint("create_issue", "422_validation", executor.LABEL_MUST_EXIST)
    steps = [
        JobStep(op_name="ensure_label_exists", params={"name": "x"}),
        JobStep(op_name="create_issue", params={"title": "t", "labels": ["x", "y"]}),
    ]
    guarded = executor.apply_learned_constraints(steps)
    assert [(s.op_name, s.params.get("name")) for s in guarded] == [
        ("ensure_label_exists", "x"), ("ensure_label_exists", "y"), ("create_issue", None),
    ]


def test_committed_demo_results_are_reproducible(tmp_path):
    committed = json.loads(RESULTS.read_text())
    rows, _ = run_scenario(labeled_issue_instructions(), workdir=tmp_path / "a")
    strip = lambda rs: [{k: r[k] for k in ("github_calls", "agent_reported_api_calls", "executed_ops")} for r in rs]  # noqa: E731
    assert strip(rows) == strip(committed["create_labeled_issue_with_memory"])
