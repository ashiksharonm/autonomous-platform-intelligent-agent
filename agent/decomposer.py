from agent.github_tools import TOOL_REGISTRY
from agent.memory import capability_memory, execution_memory
from agent.models import JobStep
from agent import llm

main_ai_rules = """You are the planning component of an autonomous GitHub agent. \
Given a natural-language instruction, you decompose it into an ordered list of steps using \
ONLY the tools in the catalog provided. Each tool call is a "step".

Rules:
- Pick a short, stable `task_type` tag for the instruction (snake_case, e.g. "create_labeled_issue", \
"stale_issue_triage", "triage_summary_by_label_and_staleness"). The SAME kind of instruction must always \
get the SAME task_type, even if wording or parameters differ, so it can be matched against history.
- If a step's tool call could fail if a later step depends on its result, mark it "blocking": true. \
Independent steps (e.g. labeling two different issues) should be "blocking": false.
- If NO tool in the catalog can accomplish part of the instruction, emit a step with \
"op_name": "NEEDS_SYNTHESIS", a "capability_request" field describing in plain English exactly what \
operation is needed and what parameters it would take, and a "requested_op_name" field: a stable, \
short snake_case identifier for this capability (e.g. "triage_summary_by_label_and_staleness"). Always \
pick the SAME requested_op_name for the same kind of gap so it can be reused once synthesized — check \
the catalog first, since a matching capability may already exist under a "(synthesized)" entry, in \
which case use ITS op_name directly instead of requesting synthesis again.
- If guidance below tells you to avoid an operation or prefer a specific sequence, follow it unless the \
instruction clearly requires otherwise.

Respond with ONLY a JSON object of this exact shape:
{
"task_type": "...",
"steps": [
{"op_name": "...", "params": {...}, "blocking": true, "capability_request": null}
]
}
"""


def get_all_tools_string():
    str_lines = []
    all_tools = capability_memory.list_all()
    
    for single_tool in all_tools:
        tool_rules = []
        if single_tool.get("known_constraints"):
            tool_rules = single_tool.get("known_constraints")
            
        basic_line = f"- {single_tool['op_name']} ({single_tool['source']}): {single_tool['signature_desc']}"
        
        if len(tool_rules) > 0:
            rule_strings = []
            for r in tool_rules:
                rule_strings.append(r["detail"])
                
            joined_rules = "; ".join(rule_strings)
            basic_line = basic_line + f" [known constraints: {joined_rules}]"
            
        str_lines.append(basic_line)
        
    return "\n".join(str_lines)


def break_down_command(user_text):
    tools_text = get_all_tools_string()
    
    prompt_for_ai = f"Tool catalog:\n{tools_text}\n\nInstruction: {user_text}\n\nRespond with the JSON object described in the system prompt."
    
    ai_reply = llm.chat_json(main_ai_rules, prompt_for_ai)
    job_category = ai_reply["task_type"]
    
    past_advice = execution_memory.derive_guidance(job_category)
    good_plan = past_advice["preferred_decomposition"]
    bad_tools = past_advice["avoid_ops"]
    
    if len(bad_tools) > 0 or good_plan:
        extra_help_text = "Memory guidance for this task_type:\n"
        
        if good_plan:
            good_tools = []
            for p in good_plan:
                good_tools.append(p['op_name'])
            extra_help_text = extra_help_text + f"- Preferred (last known-good) step sequence: {good_tools}\n"
            
        if len(bad_tools) > 0:
            extra_help_text = extra_help_text + f"- Avoid these ops if an alternative exists, they have failed often for this task: {bad_tools}\n"
            
        ai_reply = llm.chat_json(main_ai_rules, prompt_for_ai + "\n\n" + extra_help_text)
        job_category = ai_reply["task_type"]
        
    final_step_list = []
    for s in ai_reply["steps"]:
        
        step_args = {}
        if "params" in s:
            step_args = s["params"]
            
        is_blocking = True
        if "blocking" in s:
            is_blocking = s["blocking"]
            
        req_cap = None
        if "capability_request" in s:
            req_cap = s["capability_request"]
            
        req_op = None
        if "requested_op_name" in s:
            req_op = s["requested_op_name"]
            
        new_step = JobStep(
            op_name=s["op_name"],
            params=step_args,
            blocking=is_blocking,
            capability_request=req_cap,
            requested_op_name=req_op,
        )
        final_step_list.append(new_step)
        
    return job_category, final_step_list