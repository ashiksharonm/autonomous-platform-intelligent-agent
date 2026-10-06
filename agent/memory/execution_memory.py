import json
from datetime import datetime, timezone

from agent.config import AVOID_OP_FAILURE_RATE
from agent.memory.db import db_cursor


def get_time_now():
    return datetime.now(timezone.utc).isoformat()


def get_past_jobs(job_name, max_results=5):
    with db_cursor() as my_cursor:
        # this will fetch the most recent jobs of a specific type from the database.
        my_cursor.execute(
            "SELECT * FROM runs WHERE task_type = ? ORDER BY started_at DESC LIMIT ?",
            (job_name, max_results),
        )
        found_runs = my_cursor.fetchall()
        
        run_list = []
        for r in found_runs:
            run_list.append(dict(r))
            
        return run_list


def get_job_parts(job_id_string):
    with db_cursor() as my_cursor:
        # this will fetch all the steps associated with a specific job.
        my_cursor.execute(
            "SELECT * FROM steps WHERE run_id = ? ORDER BY step_index", 
            (job_id_string,)
        )
        found_steps = my_cursor.fetchall()
        
        step_list = []
        for s in found_steps:
            step_list.append(dict(s))
            
        return step_list


def derive_guidance(job_name):
    past_jobs = get_past_jobs(job_name, 10)
    # this will analyze past jobs to provide advice on preferred decomposition and tools to avoid.
    
    if len(past_jobs) == 0:
        return {"preferred_decomposition": None, "avoid_ops": []}

    best_plan = None
    for job in past_jobs:
        if job["outcome"] == "success":
            best_plan = json.loads(job["decomposition_json"])
            break

    tool_scores = {}
    for job in past_jobs:
        job_steps = get_job_parts(job["run_id"])
        
        for step in job_steps:
            tool = step["op_name"]
            
            if tool not in tool_scores:
                tool_scores[tool] = {"good": 0, "bad": 0}
                
            if step["status"] == "failed":
                tool_scores[tool]["bad"] += 1
            elif step["status"] == "ok":
                tool_scores[tool]["good"] += 1

    bad_tools = []
    for tool in tool_scores:
        good_count = tool_scores[tool]["good"]
        bad_count = tool_scores[tool]["bad"]
        total_uses = good_count + bad_count
        
        if total_uses > 0:
            fail_percent = bad_count / total_uses
            if fail_percent > AVOID_OP_FAILURE_RATE:
                bad_tools.append(tool)

    return {"preferred_decomposition": best_plan, "avoid_ops": bad_tools}


def create_job_entry(run_id, raw_instruction, task_type, decomposition, started_at):
    with db_cursor() as my_cursor:
        # this will create a new job entry in the database with the initial details and mark it as in progress.
        my_cursor.execute(
            """INSERT INTO runs (run_id, raw_instruction, task_type, decomposition_json,
                                  outcome, total_api_calls, total_latency_ms, started_at, finished_at)
               VALUES (?, ?, ?, ?, 'in_progress', 0, 0, ?, NULL)""",
            (run_id, raw_instruction, task_type, json.dumps(decomposition), started_at),
        )


def finalize_run(run_id, outcome, total_api_calls, total_latency_ms, finished_at):
    with db_cursor() as my_cursor:
        # this will update the job entry in the database to mark it as completed with the final outcome and stats.
        my_cursor.execute(
            """UPDATE runs SET outcome = ?, total_api_calls = ?, total_latency_ms = ?, finished_at = ?
               WHERE run_id = ?""",
            (outcome, total_api_calls, total_latency_ms, finished_at, run_id),
        )


def record_run(run_id, raw_instruction, task_type, decomposition, outcome, total_api_calls, total_latency_ms, started_at, finished_at):
    with db_cursor() as my_cursor:
        # this will save a job entry in the database that was completed quickly without needing to mark it as in progress first.
        my_cursor.execute(
            """INSERT INTO runs (run_id, raw_instruction, task_type, decomposition_json,
                                  outcome, total_api_calls, total_latency_ms, started_at, finished_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (run_id, raw_instruction, task_type, json.dumps(decomposition),
             outcome, total_api_calls, total_latency_ms, started_at, finished_at),
        )


def record_step(step_id, run_id, step_index, op_name, params,
                 status, api_calls, latency_ms, error_class=None, agent_decision=None):
    with db_cursor() as my_cursor:
        # this will save a specific step of a job in the database with all relevant details including status and any errors.
        my_cursor.execute(
            """INSERT INTO steps (step_id, run_id, step_index, op_name, params_json,
                                   status, error_class, api_calls, latency_ms, agent_decision)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (step_id, run_id, step_index, op_name, json.dumps(params),
             status, error_class, api_calls, latency_ms, agent_decision),
        )


def get_stats_for_task_type(job_name):
    with db_cursor() as my_cursor:
        # this will fetch summary statistics for past jobs of a specific type, including run IDs, start times, outcomes, and performance metrics.
        my_cursor.execute(
            """SELECT run_id, started_at, outcome, total_api_calls, total_latency_ms
               FROM runs WHERE task_type = ? ORDER BY started_at ASC""",
            (job_name,),
        )
        found_rows = my_cursor.fetchall()
        
        stats_list = []
        for r in found_rows:
            stats_list.append(dict(r))
            
        return stats_list