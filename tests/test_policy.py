import json

import pytest

import agent.memory.db as db_module
from agent import config, core, github_tools, llm, mock_llm
from agent.models import JobStep
from agent.policy import AuditLog, PolicyError, evaluate_plan, parse_policy, render_plan, verify_audit_log

POLICY_YAML = """
version: 1
allowed_repos: ["demo/*"]
allowed_ops: [create_issue, ensure_label_exists, add_labels_to_issue, comment_on_issue, find_stale_labeled_issues]
read_only_ops: [find_stale_labeled_issues]
max_blast_radius: 3
approve_above: 2
require_approval: [comment_on_issue]
synthesized_code: require_approval
"""


def steps(*ops):
    return [JobStep(op_name=op, params={}) for op in ops]


# --- policy parsing ---------------------------------------------------------
def test_parse_valid_policy():
    p = parse_policy(POLICY_YAML)
    assert p.repo_allowed("demo/x") and not p.repo_allowed("other/x")
    assert p.max_blast_radius == 3 and p.approve_above == 2
    assert p.is_write("create_issue") and not p.is_write("find_stale_labeled_issues")
    assert p.is_write("NEEDS_SYNTHESIS")  # unknown ops fail closed


@pytest.mark.parametrize(
    "text, fragment",
    [
        ("- a\n- b", "mapping"),
        ("allowed_ops: [create_issue]", "allowed_repos"),
        ("allowed_repos: [a]\nbogus: 1", "unknown policy keys"),
        ("allowed_repos: a", "list of non-empty strings"),
        ("allowed_repos: [a]\nmax_blast_radius: -1", "integer"),
        ("allowed_repos: [a]\nmax_blast_radius: true", "integer"),
        ("allowed_repos: [a]\nsynthesized_code: maybe", "synthesized_code"),
        ("allowed_repos: [a]\nversion: 2", "version"),
        ("allowed_repos: [a\n", "invalid YAML"),
    ],
)
def test_malformed_policy_is_rejected(text, fragment):
    with pytest.raises(PolicyError, match=fragment):
        parse_policy(text)


def test_missing_policy_file(tmp_path):
    from agent.policy import load_policy

    with pytest.raises(PolicyError, match="cannot read"):
        load_policy(tmp_path / "nope.yaml")


# --- plan evaluation --------------------------------------------------------
def test_simple_plan_is_allowed():
    d = evaluate_plan(parse_policy(POLICY_YAML), "demo/r", steps("create_issue"))
    assert d.verdict == "allow" and d.blast_radius == 1


def test_repo_outside_allowlist_is_denied():
    d = evaluate_plan(parse_policy(POLICY_YAML), "evil/r", steps("create_issue"))
    assert d.verdict == "deny" and "allowed_repos" in d.plan_reasons[0]


def test_op_not_allowed_is_denied():
    d = evaluate_plan(parse_policy(POLICY_YAML), "demo/r", steps("close_all_issues"))
    assert d.verdict == "deny" and d.steps[0].verdict == "deny"


def test_blast_radius_counts_writes_only():
    p = parse_policy(POLICY_YAML)
    reads = evaluate_plan(p, "demo/r", steps(*["find_stale_labeled_issues"] * 10))
    assert reads.verdict == "allow" and reads.blast_radius == 0
    over = evaluate_plan(p, "demo/r", steps(*["create_issue"] * 4))
    assert over.verdict == "deny" and over.blast_radius == 4


def test_approval_rules():
    p = parse_policy(POLICY_YAML)
    assert evaluate_plan(p, "demo/r", steps("comment_on_issue")).verdict == "needs_approval"
    d = evaluate_plan(p, "demo/r", steps("create_issue", "create_issue", "create_issue"))
    assert d.verdict == "needs_approval" and "approve_above" in d.plan_reasons[0]


def test_deny_beats_approval():
    d = evaluate_plan(parse_policy(POLICY_YAML), "demo/r", steps("comment_on_issue", "close_all_issues"))
    assert d.verdict == "deny"


@pytest.mark.parametrize("mode, verdict", [("deny", "deny"), ("require_approval", "needs_approval"), ("allow", "allow")])
def test_synthesized_code_modes(mode, verdict):
    p = parse_policy(POLICY_YAML.replace("synthesized_code: require_approval", f"synthesized_code: {mode}"))
    assert evaluate_plan(p, "demo/r", steps("NEEDS_SYNTHESIS")).verdict == verdict
    # an already-stored synthesized capability is treated the same way
    assert evaluate_plan(p, "demo/r", steps("my_cap"), synthesized_ops={"my_cap"}).verdict == verdict


def test_render_plan_mentions_every_step():
    d = evaluate_plan(parse_policy(POLICY_YAML), "demo/r", steps("create_issue", "close_all_issues"))
    text = render_plan("req_1", "do things", d)
    assert "req_1" in text and "DENY" in text and "create_issue" in text and "close_all_issues" in text


# --- audit log --------------------------------------------------------------
def test_audit_chain_verifies_and_detects_tampering(tmp_path):
    log = AuditLog(tmp_path / "a.jsonl")
    for i in range(3):
        log.append("req_1", "step", index=i)
    assert verify_audit_log(log.path)[0]
    lines = log.path.read_text().splitlines()

    record = json.loads(lines[1])
    record["index"] = 99
    edited = [lines[0], json.dumps(record, sort_keys=True), lines[2]]
    log.path.write_text("\n".join(edited) + "\n")
    ok, msg = verify_audit_log(log.path)
    assert not ok and "line 2" in msg

    log.path.write_text("\n".join([lines[0], lines[2]]) + "\n")  # deleted a middle record
    assert not verify_audit_log(log.path)[0]


def test_audit_appends_across_instances_and_filters(tmp_path):
    AuditLog(tmp_path / "a.jsonl").append("req_a", "request")
    AuditLog(tmp_path / "a.jsonl").append("req_b", "request")
    log = AuditLog(tmp_path / "a.jsonl")
    assert [r["seq"] for r in log.read()] == [1, 2]
    assert len(log.read("req_b")) == 1 and verify_audit_log(log.path)[0]
    assert (log.path.stat().st_mode & 0o777) == 0o600


def test_verify_rejects_garbage_and_missing(tmp_path):
    assert not verify_audit_log(tmp_path / "missing")[0]
    (tmp_path / "bad").write_text("not json\n")
    assert not verify_audit_log(tmp_path / "bad")[0]


# --- end to end through core (mock GitHub + mock planner) -------------------
@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", str(tmp_path / "m.sqlite"))
    monkeypatch.setattr(config, "GITHUB_MODE", "mock")
    monkeypatch.setattr(config, "GITHUB_REPO", "")
    monkeypatch.setattr(config, "LLM_PROVIDER", "mock")
    github_tools.reset_session()
    yield {"audit": AuditLog(tmp_path / "audit.jsonl"), "policy": parse_policy(POLICY_YAML)}
    github_tools.reset_session()


def run(env, text, **kw):
    return core.start_agent_job(text, policy=env["policy"], audit=env["audit"], **kw)


def gh_calls():
    return github_tools.create_gh_session().calls


def test_allowed_request_executes_and_is_audited(env):
    out = run(env, "Create an issue titled 'Hi' with label 'bug'", request_id="req_fixed")
    assert out.outcome == "success" and out.request_id == "req_fixed"
    events = [r["event"] for r in env["audit"].read("req_fixed")]
    assert events == ["request", "plan", "step", "outcome"]
    assert verify_audit_log(env["audit"].path)[0]


def test_denied_plan_executes_nothing(env):
    out = run(env, "Create 6 issues titled 'x'")
    assert out.outcome == core.BLOCKED and out.step_results == []
    assert gh_calls() == []
    assert [r["event"] for r in env["audit"].read(out.request_id)][-1] == "blocked"


def test_unknown_destructive_op_is_denied_before_any_call(env):
    out = run(env, "Close all issues in the repo")
    assert out.outcome == core.BLOCKED and gh_calls() == []


def test_repo_not_allowed(env, monkeypatch):
    monkeypatch.setattr(config, "GITHUB_REPO", "someone/else")
    out = run(env, "Create an issue titled 'Hi'")
    assert out.outcome == core.BLOCKED and "allowed_repos" in out.plan_text


def test_dry_run_plans_but_never_touches_github(env):
    out = run(env, "Create an issue titled 'Hi'", dry_run=True)
    assert out.outcome == core.DRY_RUN and "create_issue" in out.to_text()
    assert gh_calls() == []


def test_approval_granted_executes(env):
    seen = []

    def approver(job):
        seen.append(job.plan_text)
        return True

    approver.name = "tester"
    out = run(env, "Comment on issue #1 saying 'looks good'", approver=approver)
    assert out.outcome == "success" and "comment_on_issue" in seen[0]
    approval = [r for r in env["audit"].read(out.request_id) if r["event"] == "approval"][0]
    assert approval["approved"] is True and approval["by"] == "tester"
    assert env["audit"].read(out.request_id)[-1]["approved_by"] == "tester"


def test_approval_rejected_executes_nothing(env):
    out = run(env, "Comment on issue #1 saying 'looks good'", approver=lambda job: False)
    assert out.outcome == core.REJECTED and gh_calls() == []


def test_missing_approver_fails_closed(env):
    out = run(env, "Comment on issue #1 saying 'x'")
    assert out.outcome == core.REJECTED and gh_calls() == []


def test_no_policy_behaves_as_before(env):
    out = core.start_agent_job("Create an issue titled 'Hi'")
    assert out.outcome == "success" and "no policy" in out.plan_text


def test_synthesized_capability_needs_approval(env):
    out = run(env, "Generate this week's triage summary")
    assert out.outcome == core.REJECTED
    approved = run(env, "Generate this week's triage summary", approver=lambda job: True)
    assert approved.outcome == "success"
    # once stored, the synthesized op is still recognised as LLM-written code
    again = run(env, "Generate this week's triage summary")
    assert again.outcome == core.REJECTED


def test_stale_triage_read_only_plan_is_allowed(env):
    out = run(env, "Find stale issues labeled 'bug'")
    assert out.outcome == "success" and out.steps[0].op_name == "find_stale_labeled_issues"


# --- mock planner and providers ---------------------------------------------
@pytest.mark.parametrize(
    "text, task, ops",
    [
        ("Create an issue titled 'A' with label 'b'", "create_labeled_issue", ["create_issue"]),
        ("create an issue titled 'A'", "create_issue", ["create_issue"]),
        ("Create 3 issues titled 'A'", "bulk_create_issues", ["create_issue"] * 3),
        ("Comment on issue #2 saying 'hi'", "comment_on_issue", ["comment_on_issue"]),
        ("Add label 'p1' to issue #3", "label_issue", ["ensure_label_exists", "add_labels_to_issue"]),
        ("Find stale issues labeled 'bug'", "stale_issue_triage", ["find_stale_labeled_issues"]),
        ("Generate a triage summary", "triage_summary_by_label_and_staleness", ["NEEDS_SYNTHESIS"]),
        ("Delete all issues", "bulk_destructive", ["close_all_issues"]),
        ("tell me a joke", "unrecognised", []),
    ],
)
def test_mock_planner(text, task, ops):
    plan = mock_llm.plan_instruction(text)
    assert plan["task_type"] == task and [s["op_name"] for s in plan["steps"]] == ops


def test_provider_resolution(monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", "auto")
    monkeypatch.setattr(config, "GROQ_API_KEY", "")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    assert llm.provider() == "mock"
    monkeypatch.setattr(config, "GEMINI_API_KEY", "k")
    assert llm.provider() == "gemini"
    monkeypatch.setattr(config, "GROQ_API_KEY", "k")
    assert llm.provider() == "groq"
    monkeypatch.setattr(config, "LLM_PROVIDER", "nope")
    with pytest.raises(ValueError):
        llm.provider()


def test_gemini_request_shape_and_parsing(monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", "gemini")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "secret")
    captured = {}

    def fake_post(url, headers, payload, timeout=60):
        captured.update(url=url, headers=headers, payload=payload)
        return {"candidates": [{"content": {"parts": [{"text": '```json\n{"task_type": "t", "steps": []}\n```'}]}}]}

    monkeypatch.setattr(llm, "_http_post_json", fake_post)
    assert llm.chat_json("sys", "user") == {"task_type": "t", "steps": []}
    assert captured["headers"] == {"x-goog-api-key": "secret"} and "secret" not in captured["url"]
    assert captured["payload"]["systemInstruction"]["parts"][0]["text"] == "sys"
