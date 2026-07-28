import ast
import time

from agent import llm
from agent.github_tools import fetch_my_repo
from agent.memory import capability_memory
from agent.models import JobStep, StepOutcome

okay_imports = {"datetime", "collections", "re"}
bad_functions = {"eval", "exec", "open", "__import__"}
bad_modules = ("os", "subprocess", "sys", "shutil")

bot_instructions = """You write a single Python function to fill a capability gap in an autonomous \
GitHub agent. Requirements:
- Signature MUST be exactly: def run(repo, params: dict) -> dict:
- `repo` is an already-authenticated PyGithub Repository object. Use only its methods.
- You may only use these modules (already available, do not import anything else): datetime, collections, re.
- Do not use eval, exec, open, __import__, os, subprocess, sys, or shutil.
- Any write/create call on `repo` (create_issue, create_comment, etc.) MUST be gated behind
  `if not params.get("dry_run"):`. In the dry_run branch, compute and return what WOULD be done
  without calling the write method, using real data fetched from `repo` (read calls are fine in
  dry_run mode).
- Return a dict with keys: "summary" (str) and "actions_taken" (list[str]).
- Respond with ONLY the Python code for the function, no prose, no markdown fences.
"""


class CodeMakerError(Exception):
    pass


def check_code_safety(source_code):
    try:
        code_tree = ast.parse(source_code)
    except SyntaxError as parse_err:
        raise CodeMakerError("syntax error: " + str(parse_err))

    found_funcs = []
    for node in code_tree.body:
        if isinstance(node, ast.FunctionDef):
            if node.name == "run":
                found_funcs.append(node)
                
    if len(found_funcs) == 0:
        raise CodeMakerError("no top-level function named 'run' found")
        
    main_func = found_funcs[0]
    
    param_names = []
    for arg_obj in main_func.args.args:
        param_names.append(arg_obj.arg)
        
    if len(param_names) < 2 or param_names[0] != "repo" or param_names[1] != "params":
        joined_names = ", ".join(param_names)
        raise CodeMakerError("function signature must be run(repo, params), got run(" + joined_names + ")")

    for node in ast.walk(code_tree):
        if isinstance(node, ast.Import) or isinstance(node, ast.ImportFrom):
            if isinstance(node, ast.Import):
                base_mod = node.names[0].name.split(".")[0]
            else:
                if node.module:
                    base_mod = node.module.split(".")[0]
                else:
                    base_mod = ""
                    
            if base_mod not in okay_imports:
                raise CodeMakerError("disallowed import: " + base_mod)
                
        if isinstance(node, ast.Call):
            func_name = getattr(node.func, "id", None)
            if func_name in bad_functions:
                raise CodeMakerError("disallowed call: " + func_name)
                
        if isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name):
                if node.value.id in bad_modules:
                    raise CodeMakerError("disallowed attribute access on: " + node.value.id)
            if node.attr.startswith("__") and node.attr.endswith("__"):
                raise CodeMakerError("disallowed dunder attribute access: " + node.attr)


def get_run_function(source_code):
    check_code_safety(source_code)
    
    local_vars = {}
    allowed_builtins = {
        "len": len, "range": range, "sorted": sorted, "enumerate": enumerate,
        "list": list, "dict": dict, "set": set, "str": str, "int": int, "float": float,
        "bool": bool, "min": min, "max": max, "sum": sum, "isinstance": isinstance,
        "getattr": getattr, "hasattr": hasattr, "zip": zip, "reversed": reversed,
        "Exception": Exception, "ValueError": ValueError, "print": print,
    }
    
    exec(source_code, {"__builtins__": allowed_builtins}, local_vars)
    
    extracted_func = local_vars.get("run")
    if extracted_func is None:
        raise CodeMakerError("generated module did not define 'run'")
        
    return extracted_func


def test_without_saving(func_to_test, current_repo, test_args):
    try:
        fake_args = dict(test_args)
        fake_args["dry_run"] = True
        
        result_data = func_to_test(current_repo, fake_args)
        
        if not isinstance(result_data, dict):
            return False, "dry run did not return {summary, actions_taken}"
        if "summary" not in result_data or "actions_taken" not in result_data:
            return False, "dry run did not return {summary, actions_taken}"
            
        if not result_data["summary"]:
            return False, "dry run summary was empty"
            
        return True, ""
    except Exception as err:
        return False, "dry run raised: " + str(err)


def create_and_save_tool(tool_name, tool_desc, args_format, try_limit=2):
    old_tool = capability_memory.get(tool_name)
    
    if old_tool is not None:
        if old_tool["source"] == "synthesized" and old_tool["code"]:
            return old_tool["code"], old_tool["signature_desc"]

    current_repo = fetch_my_repo()
    
    if args_format:
        expected_args = str(args_format)
    else:
        expected_args = "{}"
        
    base_prompt = "Capability needed: " + tool_desc + "\nExpected params shape: " + expected_args
    error_msg = ""

    for attempt_num in range(try_limit):
        if attempt_num == 0:
            current_prompt = base_prompt
        else:
            current_prompt = base_prompt + "\n\nPrevious attempt failed: " + error_msg + "\nFix it."
            
        generated_code = llm.generate_code(bot_instructions, current_prompt)
        
        try:
            check_code_safety(generated_code)
            new_func = get_run_function(generated_code)
            
            is_safe, fail_reason = test_without_saving(new_func, current_repo, {})
            
            if not is_safe:
                error_msg = fail_reason
                continue
                
            final_desc = "(synthesized) " + tool_desc
            capability_memory.register_synthesized(tool_name, generated_code, final_desc)
            
            return generated_code, final_desc
            
        except CodeMakerError as err:
            error_msg = str(err)

    raise CodeMakerError("synthesis failed after " + str(try_limit) + " attempts: " + error_msg)


def make_tool_and_run_it(current_step):
    start_time = time.monotonic()
    target_tool = current_step.requested_op_name
    
    if not target_tool:
        time_taken = int((time.monotonic() - start_time) * 1000)
        return StepOutcome(
            step=current_step, 
            status="failed", 
            api_calls=0, 
            latency_ms=time_taken,
            error_class="synthesis_failed", 
            error_message="no requested_op_name provided for synthesis",
            agent_decision="decomposer did not supply a stable op_name for this capability gap; cannot register a reusable capability without one",
        )

    try:
        tool_code, tool_info = create_and_save_tool(target_tool, current_step.capability_request, current_step.param_schema)
    except CodeMakerError as err:
        time_taken = int((time.monotonic() - start_time) * 1000)
        
        fallback_desc = "unknown"
        if current_step.capability_request:
            fallback_desc = current_step.capability_request
            
        capability_memory.ensure_builtin(target_tool, fallback_desc)
        capability_memory.record_outcome(target_tool, False, 0, time_taken, "synthesis_failed")
        
        return StepOutcome(
            step=current_step, 
            status="failed", 
            api_calls=0, 
            latency_ms=time_taken,
            error_class="synthesis_failed", 
            error_message=str(err),
            agent_decision="could not synthesize a working implementation after retries; reporting the gap instead of guessing",
        )

    func_to_run = get_run_function(tool_code)
    active_repo = fetch_my_repo()
    
    try:
        actual_args = dict(current_step.params)
        actual_args["dry_run"] = False
        
        func_result = func_to_run(active_repo, actual_args)
        
        time_taken = int((time.monotonic() - start_time) * 1000)
        capability_memory.record_outcome(target_tool, True, 1, time_taken)
        
        return StepOutcome(
            step=current_step, 
            status="ok", 
            api_calls=1, 
            latency_ms=time_taken, 
            artifacts=func_result
        )
        
    except Exception as err:
        time_taken = int((time.monotonic() - start_time) * 1000)
        capability_memory.record_outcome(target_tool, False, 1, time_taken, "unknown")
        
        err_text = str(err)
        if len(err_text) > 500:
            err_text = err_text[:500]
            
        return StepOutcome(
            step=current_step, 
            status="failed", 
            api_calls=1, 
            latency_ms=time_taken,
            error_class="unknown", 
            error_message=err_text,
            agent_decision="synthesized function raised on real execution; step marked failed",
        )