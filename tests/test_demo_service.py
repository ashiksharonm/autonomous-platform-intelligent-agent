import pytest

from agent import config, github_tools, llm
from agent.demo_service import SAMPLES, DemoService


@pytest.fixture
def svc(monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", "mock")
    monkeypatch.setattr(config, "GITHUB_REPO", "")
    service = DemoService()
    yield service
    github_tools.reset_session()


def test_each_sample_hits_its_advertised_verdict(svc):
    expected = {
        "Allowed": "executed",
        "Needs approval": "awaiting_approval",
        "Denied": "blocked_by_policy",
    }
    for name, text in SAMPLES:
        result = svc.submit(text, svc.default_policy)
        assert result["status"] == expected[name.split(":")[0]], name


def test_approve_then_reject_flow(svc):
    pending = svc.submit("Comment on issue #1 saying 'hi'", svc.default_policy)
    assert pending["status"] == "awaiting_approval" and "1 API calls" not in svc.repo_text()
    done = svc.decide(pending["request_id"], True)
    assert done["status"] == "executed" and "comment_on_issue -> ok" in done["report"]
    assert svc.decide(pending["request_id"], True)["status"] == "error"  # cannot decide twice

    second = svc.submit("Comment on issue #2 saying 'no'", svc.default_policy)
    assert svc.decide(second["request_id"], False)["status"] == "rejected_by_human"
    assert "[chain OK]" in svc.audit_text()


def test_editing_the_policy_changes_the_outcome(svc):
    text = "Create 6 issues titled 'Spam'"
    assert svc.submit(text, svc.default_policy)["status"] == "blocked_by_policy"
    relaxed = svc.default_policy.replace("max_blast_radius: 4", "max_blast_radius: 10").replace("approve_above: 2", "approve_above: 10")
    assert svc.submit(text, relaxed)["status"] == "executed"


def test_bad_input_and_reset(svc):
    assert svc.submit("  ", svc.default_policy)["status"] == "error"
    assert "Policy error" in svc.submit("x", "allowed_ops: []")["plan"]
    svc.submit("Create an issue titled 'Hi'", svc.default_policy)
    assert "[chain OK]" in svc.audit_text() and "request" in svc.audit_text()
    svc.reset()
    assert svc.audit_text().startswith("[chain OK] empty")
    assert llm.provider() == "mock"
