import argparse
import json
import sys

from agent import core
from agent.memory import capability_memory, execution_memory
from agent.memory.db import setup_tables
from agent.policy import AuditLog, PolicyError, load_policy, verify_audit_log

GATED_OUTCOMES = (core.BLOCKED, core.REJECTED)


def make_cli_approver(auto_yes):
    def approver(job):
        print(job.plan_text)
        if auto_yes:
            return True
        if not sys.stdin.isatty():
            print("Approval required but stdin is not a terminal; rejecting (use --yes to pre-approve).")
            return False
        return input("Approve and execute this plan? [y/N] ").strip().lower() in ("y", "yes")

    approver.name = "cli:--yes" if auto_yes else "cli:tty"
    return approver


def execute_user_command(user_text, args):
    policy = load_policy(args.policy) if args.policy else None
    audit = AuditLog(args.audit_log) if policy and args.audit_log else None
    final_out = core.start_agent_job(
        user_text,
        policy=policy,
        approver=make_cli_approver(args.yes),
        dry_run=args.dry_run,
        audit=audit,
        request_id=args.request_id,
    )
    print(final_out.to_text())

    if final_out.outcome in GATED_OUTCOMES:
        sys.exit(3)
    if final_out.outcome not in ("success", core.DRY_RUN):
        sys.exit(1)


def verify_audit(path):
    ok, message = verify_audit_log(path)
    print(("OK: " if ok else "TAMPERED/INVALID: ") + message)
    sys.exit(0 if ok else 1)


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
    parser_for_run.add_argument("--policy", help="YAML policy evaluated before any write (see policies/default.yaml)")
    parser_for_run.add_argument("--dry-run", action="store_true", help="plan and evaluate the policy, execute nothing")
    parser_for_run.add_argument("--yes", action="store_true", help="pre-approve plans that need human approval")
    parser_for_run.add_argument("--audit-log", default="data/audit.jsonl", help="append-only JSONL audit log (with --policy)")
    parser_for_run.add_argument("--request-id", default=None)

    parser_for_audit = sub_parsers.add_parser("verify-audit", help="Check an audit log's hash chain")
    parser_for_audit.add_argument("path", nargs="?", default="data/audit.jsonl")

    parser_for_stats = sub_parsers.add_parser("stats", help="Show logged runs for a task_type")
    parser_for_stats.add_argument("task_type", type=str)

    sub_parsers.add_parser("show-capabilities", help="List known capabilities and their stats")

    parsed_input = my_parser.parse_args()

    if parsed_input.command == "run":
        try:
            execute_user_command(parsed_input.instruction, parsed_input)
        except PolicyError as exc:
            print(f"policy error: {exc}", file=sys.stderr)
            sys.exit(2)
    elif parsed_input.command == "verify-audit":
        verify_audit(parsed_input.path)
    elif parsed_input.command == "stats":
        print_job_history(parsed_input.task_type)
    elif parsed_input.command == "show-capabilities":
        display_all_tools()


if __name__ == "__main__":
    start_cli()