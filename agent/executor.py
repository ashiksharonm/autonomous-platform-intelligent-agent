import time
import uuid

from github import GithubException

from agent.github_tools import TOOL_REGISTRY, fetch_my_repo
from agent.memory import capability_memory, execution_memory
from agent.models import JobStep, StepOutcome

error_actions = {
    "422_validation": "step failed validation; will attempt a targeted fix (e.g. create missing label) and retry once before giving up on this step",
    "404_not_found": "referenced resource does not exist; skipping this step rather than retrying",
    "403_permission": "insufficient permissions for this operation; aborting run rather than silently skipping a write the user asked for",
    "rate_limited": "hit a GitHub rate limit; recording constraint for future runs and aborting this step rather than retrying blindly",
    "synthesis_failed": "could not synthesize a working implementation after retries; reporting the gap instead of guessing",
    "unknown": "unexpected error; step marked failed, no automatic retry attempted",
}


def figure_out_error(err):
    if isinstance(err, GithubException):
        if err.status == 422:
            return "422_validation"
        if err.status == 404:
            return "404_not_found"
        if err.status == 403:
            return "403_permission"
        if err.status == 429:
            return "rate_limited"
    return "unknown"


def check_past_mistakes(job_type, step_list):
    # this will analyze past jobs of the same type to provide advice on preferred decomposition and tools to avoid.
    past_info = execution_memory.derive_guidance(job_type)
    bad_tools = past_info["avoid_ops"]
    good_plan = past_info["preferred_decomposition"]

    current_tools = []
    for s in step_list:
        current_tools.append(s.op_name)
        
    found_bad_tool = False
    for bt in bad_tools:
        if bt in current_tools:
            found_bad_tool = True
            break
            
    if found_bad_tool and good_plan:
        new_steps = []
        for p in good_plan:
            is_blocking = True
            if "blocking" in p:
                is_blocking = p["blocking"]
            
            step_args = {}
            if "params" in p:
                step_args = p["params"]
                
            new_steps.append(JobStep(op_name=p["op_name"], params=step_args, blocking=is_blocking))
        return new_steps
        
    return step_list


def run_single_step(current_step):
    # this will execute a single step, handling both built-in tools and synthesized capabilities, while tracking API usage and latency.
    from agent import synthesis
    
    start_time = time.monotonic()
    calls_left_start = get_api_quota()

    if current_step.op_name == "NEEDS_SYNTHESIS":
        out_res = synthesis.synthesize_and_run(current_step)
        time_taken = int((time.monotonic() - start_time) * 1000)
        
        if out_res.latency_ms:
            return out_res
        else:
            return add_time_taken(out_res, time_taken)

    tool_info = TOOL_REGISTRY.get(current_step.op_name)
    db_tool = capability_memory.get(current_step.op_name)

    try:
        if tool_info is not None:
            func_to_run = tool_info["fn"]
            generated_data = func_to_run(fetch_my_repo(), current_step.params)
        elif db_tool is not None and db_tool["source"] == "synthesized":
            func_to_run = synthesis.load_run_function(db_tool["code"])
            raw_out = func_to_run(fetch_my_repo(), current_step.params)
            
            if isinstance(raw_out, dict):
                generated_data = raw_out
            else:
                generated_data = {"result": raw_out}
        else:
            raise RuntimeError(f"No tool or synthesized capability registered for '{current_step.op_name}'")

        time_taken = int((time.monotonic() - start_time) * 1000)
        used_calls = calculate_used_calls(calls_left_start)
        capability_memory.record_outcome(current_step.op_name, True, used_calls, time_taken)
        
        return StepOutcome(
            step=current_step, 
            status="ok", 
            api_calls=used_calls, 
            latency_ms=time_taken,
            artifacts=generated_data
        )

    except Exception as err:
        time_taken = int((time.monotonic() - start_time) * 1000)
        used_calls = calculate_used_calls(calls_left_start)
        err_type = figure_out_error(err)
        
        ai_choice = error_actions.get(err_type, error_actions["unknown"])

        capability_memory.record_outcome(current_step.op_name, False, used_calls, time_taken, err_type)

        if err_type == "422_validation":
            if current_step.op_name == "create_issue":
                if "labels" in current_step.params:
                    fixed_res = fix_missing_label_bug(current_step)
                    if fixed_res is not None:
                        fixed_res.agent_decision = ai_choice + " (recovered: created missing label then retried)"
                        return fixed_res

        capability_memory.add_constraint(current_step.op_name, err_type, str(err)[:200])

        return StepOutcome(
            step=current_step, 
            status="failed", 
            api_calls=used_calls, 
            latency_ms=time_taken,
            error_class=err_type, 
            error_message=str(err)[:500], 
            agent_decision=ai_choice
        )


def fix_missing_label_bug(current_step):
    # this will attempt to recover from a missing label error by creating the label and retrying the step.
    my_repo = fetch_my_repo()
    start_time = time.monotonic()
    calls_left_start = get_api_quota()
    
    try:
        step_labels = []
        if "labels" in current_step.params:
            step_labels = current_step.params["labels"]
            
        for lbl in step_labels:
            try:
                my_repo.get_label(lbl)
            except GithubException as g_err:
                if g_err.status != 404:
                    raise
                my_repo.create_label(name=lbl, color="ededed")
                capability_memory.add_constraint(
                    "create_issue", "422_validation",
                    "label must exist before use in create_issue call"
                )
                
        issue_body = ""
        if "body" in current_step.params:
            issue_body = current_step.params["body"]
            
        new_issue = my_repo.create_issue(
            title=current_step.params["title"],
            body=issue_body,
            labels=step_labels,
        )
        
        time_taken = int((time.monotonic() - start_time) * 1000)
        used_calls = calculate_used_calls(calls_left_start)
        capability_memory.record_outcome("create_issue", True, used_calls, time_taken)
        
        return StepOutcome(
            step=current_step, 
            status="ok", 
            api_calls=used_calls, 
            latency_ms=time_taken,
            artifacts={"issue_url": new_issue.html_url, "issue_number": new_issue.number}
        )
    except Exception:
        return None


def get_api_quota():
    from agent.github_tools import calls_remaining
    try:
        return calls_remaining()
    except Exception:
        return -1


def calculate_used_calls(start_amount):
    # this will calculate the number of API calls used during a step by comparing the starting and ending quota.
    from agent.github_tools import calls_remaining
    try:
        end_amount = calls_remaining()
        if start_amount < 0 or end_amount < 0:
            return 1
            
        diff = start_amount - end_amount
        if diff > 1:
            return diff
        else:
            return 1
    except Exception:
        return 1


def add_time_taken(res_obj, ms_val):
    res_obj.latency_ms = ms_val
    return res_obj


def new_step_id():
    return str(uuid.uuid4())