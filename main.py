import argparse
import json
import sys

from agent import core
from agent.memory import capability_memory, execution_memory
from agent.memory.db import setup_tables


def execute_user_command(user_text):
    final_out = core.start_agent_job(user_text)
    print(final_out.to_text())
    
    if final_out.outcome != "success":
        sys.exit(1)


def print_job_history(job_name):
    setup_tables()
    history_rows = execution_memory.get_stats_for_task_type(job_name)
    
    if len(history_rows) == 0:
        print("No runs recorded yet for task_type='" + str(job_name) + "'")
    else:
        header_str = "started_at".ljust(30) + "outcome".ljust(16) + "api_calls".ljust(10) + "latency_ms".ljust(10)
        print(header_str)
        
        for row_data in history_rows:
            time_col = str(row_data["started_at"]).ljust(30)
            status_col = str(row_data["outcome"]).ljust(16)
            calls_col = str(row_data["total_api_calls"]).ljust(10)
            time_taken_col = str(row_data["total_latency_ms"]).ljust(10)
            
            print(time_col + status_col + calls_col + time_taken_col)


def display_all_tools():
    setup_tables()
    all_tools = capability_memory.list_all()
    
    for tool_item in all_tools:
        print("- " + str(tool_item["op_name"]) + " (" + str(tool_item["source"]) + ")")
        print("    " + str(tool_item["signature_desc"]))
        
        stats_line = "    success=" + str(tool_item["success_count"]) + " failure=" + str(tool_item["failure_count"])
        stats_line = stats_line + " success_rate=" + str(tool_item["success_rate"]) + " avg_api_calls=" + str(tool_item["avg_api_calls"])
        print(stats_line)
        
        if tool_item["known_constraints"]:
            rules_str = json.dumps(tool_item["known_constraints"])
            print("    constraints: " + rules_str)


def start_cli():
    my_parser = argparse.ArgumentParser(description="Autonomous GitHub platform agent")
    sub_parsers = my_parser.add_subparsers(dest="command", required=True)

    parser_for_run = sub_parsers.add_parser("run", help="Execute a natural language instruction")
    parser_for_run.add_argument("instruction", type=str)

    parser_for_stats = sub_parsers.add_parser("stats", help="Show logged runs for a task_type")
    parser_for_stats.add_argument("task_type", type=str)

    sub_parsers.add_parser("show-capabilities", help="List known capabilities and their stats")

    parsed_input = my_parser.parse_args()

    if parsed_input.command == "run":
        execute_user_command(parsed_input.instruction)
    elif parsed_input.command == "stats":
        print_job_history(parsed_input.task_type)
    elif parsed_input.command == "show-capabilities":
        display_all_tools()


if __name__ == "__main__":
    start_cli()