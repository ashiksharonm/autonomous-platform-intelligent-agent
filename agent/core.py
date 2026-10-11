import uuid
from datetime import datetime, timezone

from agent import decomposer, executor, report
from agent.github_tools import TOOL_REGISTRY
from agent.memory import capability_memory, execution_memory
from agent.memory.db import setup_tables


def get_time_now():
    return datetime.now(timezone.utc).isoformat()


def load_default_tools():
    for tool_key, tool_info in TOOL_REGISTRY.items():
        capability_memory.ensure_builtin(tool_key, tool_info["signature_desc"])


def start_agent_job(user_command):
    setup_tables()
    load_default_tools()

    current_job_id = str(uuid.uuid4())
    time_started = get_time_now()

    job_category, plan_steps = decomposer.break_down_command(user_command)
    safe_steps = executor.apply_memory_guard(job_category, plan_steps)

    parsed_plan = []
    for s in safe_steps:
        parsed_plan.append({
            "op_name": s.op_name, 
            "params": s.params, 
            "blocking": s.blocking
        })

    execution_memory.create_job_entry(
        run_id=current_job_id,
        raw_instruction=user_command,
        task_type=job_category,
        decomposition=parsed_plan,
        started_at=time_started,
    )

    all_results = []
    for step_idx, single_step in enumerate(safe_steps):
        step_out = executor.run_single_step(single_step)
        all_results.append(step_out)
        
        execution_memory.record_step(
            step_id=executor.new_step_id(),
            run_id=current_job_id,
            step_index=step_idx,
            op_name=single_step.op_name,
            params=single_step.params,
            status=step_out.status,
            api_calls=step_out.api_calls,
            latency_ms=step_out.latency_ms,
            error_class=step_out.error_class,
            agent_decision=step_out.agent_decision,
        )
        
        if step_out.status == "failed":
            if single_step.blocking:
                break

    time_ended = get_time_now()
    
    final_summary = report.build(
        current_job_id, 
        user_command, 
        job_category, 
        safe_steps, 
        all_results
    )

    execution_memory.finalize_run(
        run_id=current_job_id,
        outcome=final_summary.outcome,
        total_api_calls=final_summary.total_api_calls,
        total_latency_ms=final_summary.total_latency_ms,
        finished_at=time_ended,
    )

    return final_summary