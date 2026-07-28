import agent.memory.db as db_module
from agent import decomposer, llm


def test_bot_planner_creates_correct_steps(temp_dir, patcher):
    patcher.setattr(db_module, "sqlite_file_location", str(temp_dir / "test.sqlite"))
    db_module.setup_tables()

    mocked_llm_reply = {
        "task_type": "create_labeled_issue",
        "steps": [
            {
                "op_name": "create_issue", 
                "params": {"title": "Fix login bug", "labels": ["needs-triage"]},
                "blocking": True, 
                "capability_request": None, 
                "requested_op_name": None
            }
        ],
    }
    
    patcher.setattr(llm, "chat_json", lambda sys_prompt, user_prompt, retries=1: mocked_llm_reply)

    job_name, plan_steps = decomposer.break_down_command("Create an issue titled 'Fix login bug' with label 'needs-triage'")

    assert job_name == "create_labeled_issue"
    assert len(plan_steps) == 1
    assert plan_steps[0].op_name == "create_issue"
    assert plan_steps[0].params["title"] == "Fix login bug"


def test_bot_planner_asks_for_new_tool(temp_dir, patcher):
    patcher.setattr(db_module, "sqlite_file_location", str(temp_dir / "test.sqlite"))
    db_module.setup_tables()

    mocked_llm_reply = {
        "task_type": "triage_summary_by_label_and_staleness",
        "steps": [
            {
                "op_name": "NEEDS_SYNTHESIS", 
                "params": {},
                "capability_request": "Group open issues by label and staleness and post as a new issue",
                "requested_op_name": "triage_summary_by_label_and_staleness", 
                "blocking": True
            }
        ],
    }
    
    patcher.setattr(llm, "chat_json", lambda sys_prompt, user_prompt, retries=1: mocked_llm_reply)

    job_name, plan_steps = decomposer.break_down_command("Generate this week's triage summary")

    assert plan_steps[0].op_name == "NEEDS_SYNTHESIS"
    assert plan_steps[0].requested_op_name == "triage_summary_by_label_and_staleness"