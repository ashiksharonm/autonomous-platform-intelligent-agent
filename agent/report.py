from agent.models import JobStep, StepOutcome, FinalReport


def get_final_status(completed_steps_list):
    if len(completed_steps_list) == 0:
        return "failure"
        
    good_count = 0
    for single_res in completed_steps_list:
        if single_res.status == "ok":
            good_count = good_count + 1
            
    if good_count == len(completed_steps_list):
        return "success"
    elif good_count > 0:
        return "partial_failure"
    else:
        return "failure"


def build(job_id_str, user_text, job_category, planned_steps, completed_steps_list):
    final_mark = get_final_status(completed_steps_list)
    
    calls_sum = 0
    time_sum = 0
    
    for step_res in completed_steps_list:
        calls_sum = calls_sum + step_res.api_calls
        time_sum = time_sum + step_res.latency_ms
        
    final_report_obj = FinalReport(
        run_id=job_id_str,
        instruction=user_text,
        task_type=job_category,
        outcome=final_mark,
        steps=planned_steps,
        step_results=completed_steps_list,
        total_api_calls=calls_sum,
        total_latency_ms=time_sum,
    )
    
    return final_report_obj