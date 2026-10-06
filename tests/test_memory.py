import agent.memory.db as db_module
from agent.memory import capability_memory, execution_memory


def test_tool_tracking_system(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", str(tmp_path / "test.sqlite"))
    db_module.setup_tables()

    capability_memory.ensure_builtin("create_issue", "Create an issue")
    tool_data = capability_memory.get("create_issue")
    
    assert tool_data["source"] == "builtin"
    assert tool_data["success_count"] == 0

    capability_memory.record_outcome("create_issue", True, api_calls=1, latency_ms=100)
    tool_data = capability_memory.get("create_issue")
    
    assert tool_data["success_count"] == 1
    assert tool_data["success_rate"] == 1.0

    capability_memory.record_outcome("create_issue", False, api_calls=1, latency_ms=50, error_class="422_validation")
    tool_data = capability_memory.get("create_issue")
    
    assert tool_data["failure_count"] == 1
    assert tool_data["success_rate"] == 0.5

    capability_memory.add_constraint("create_issue", "422_validation", "label must exist before use")
    tool_data = capability_memory.get("create_issue")
    
    assert len(tool_data["known_constraints"]) == 1
    
    capability_memory.add_constraint("create_issue", "422_validation", "label must exist before use")
    tool_data = capability_memory.get("create_issue")
    
    assert len(tool_data["known_constraints"]) == 1


def test_ai_learns_from_history(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", str(tmp_path / "test.sqlite"))
    db_module.setup_tables()

    job_category = "create_labeled_issue"

    execution_memory.record_run(
        run_id="job_1", 
        raw_instruction="x", 
        task_type=job_category,
        decomposition=[{"op_name": "ensure_label_exists", "params": {}}],
        outcome="failure", 
        total_api_calls=1, 
        total_latency_ms=100,
        started_at="2026-01-01T00:00:00", 
        finished_at="2026-01-01T00:00:01",
    )
    execution_memory.record_step("step_1", "job_1", 0, "ensure_label_exists", {}, "failed", 1, 100, "422_validation")

    execution_memory.record_run(
        run_id="job_2", 
        raw_instruction="x", 
        task_type=job_category,
        decomposition=[{"op_name": "create_issue", "params": {}}],
        outcome="success", 
        total_api_calls=1, 
        total_latency_ms=80,
        started_at="2026-01-02T00:00:00", 
        finished_at="2026-01-02T00:00:01",
    )
    execution_memory.record_step("step_2", "job_2", 0, "create_issue", {}, "ok", 1, 80)

    past_advice = execution_memory.derive_guidance(job_category)
    
    assert past_advice["avoid_ops"] == ["ensure_label_exists"]
    assert past_advice["preferred_decomposition"] == [{"op_name": "create_issue", "params": {}}]