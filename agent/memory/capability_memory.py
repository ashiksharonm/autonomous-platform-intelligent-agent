import json
from datetime import datetime, timezone
from agent.memory.db import db_cursor


def get_current_time_str():
    return datetime.now(timezone.utc).isoformat()


def ensure_builtin(tool_name, sig_info):
    with db_cursor() as cursor:
        # we can just ignore if it's already in the database.
        cursor.execute(
            """INSERT INTO capabilities (op_name, source, code, signature_desc, created_at)
               VALUES (?, 'builtin', NULL, ?, ?)
               ON CONFLICT(op_name) DO NOTHING""",
            (tool_name, sig_info, get_current_time_str()),
        )


def get(tool_name):
    with db_cursor() as cursor:
        # this will fetch the tool's data if it exists.
        cursor.execute("SELECT * FROM capabilities WHERE op_name = ?", (tool_name,))
        result = cursor.fetchone()
        
        if result:
            return format_db_row(result)
        return None


def list_all():
    # this will fetch all tools into the database.
    with db_cursor() as cursor:
        cursor.execute("SELECT * FROM capabilities ORDER BY op_name")
        rows = cursor.fetchall()
        
        parsed_tools = []
        for r in rows:
            parsed_tools.append(format_db_row(r))
            
        return parsed_tools


def format_db_row(raw_row):
    row_data = dict(raw_row)
    
    # this will handle the JSON parsing safely
    if row_data["known_constraints"]:
        row_data["known_constraints"] = json.loads(row_data["known_constraints"])
    else:
        row_data["known_constraints"] = []
        
    total_runs = row_data["success_count"] + row_data["failure_count"]
    
    # this will avoid divide by zero errors
    if total_runs > 0:
        row_data["success_rate"] = row_data["success_count"] / total_runs
        row_data["avg_api_calls"] = row_data["total_api_calls"] / total_runs
    else:
        row_data["success_rate"] = None
        row_data["avg_api_calls"] = None
        
    return row_data


def register_synthesized(tool_name, custom_code, sig_info):
    with db_cursor() as cursor:
        # this will insert or update the tool's code and signature in the database.
        cursor.execute(
            """INSERT INTO capabilities (op_name, source, code, signature_desc, created_at)
               VALUES (?, 'synthesized', ?, ?, ?)
               ON CONFLICT(op_name) DO UPDATE SET 
                   code = excluded.code,
                   signature_desc = excluded.signature_desc""",
            (tool_name, custom_code, sig_info, get_current_time_str()),
        )


def record_outcome(tool_name, is_success, api_calls, latency_ms, error_class=None):
    with db_cursor() as cursor:
        # this will update the tool's statistics based on the run outcome.
        if is_success:
            cursor.execute(
                """UPDATE capabilities
                   SET success_count = success_count + 1,
                       total_api_calls = total_api_calls + ?,
                       total_latency_ms = total_latency_ms + ?,
                       last_used_at = ?
                   WHERE op_name = ?""",
                (api_calls, latency_ms, get_current_time_str(), tool_name),
            )
        else:
            cursor.execute(
                """UPDATE capabilities
                   SET failure_count = failure_count + 1,
                       total_api_calls = total_api_calls + ?,
                       total_latency_ms = total_latency_ms + ?,
                       last_used_at = ?,
                       last_error_class = ?
                   WHERE op_name = ?""",
                (api_calls, latency_ms, get_current_time_str(), error_class, tool_name),
            )


def add_constraint(tool_name, rule_kind, rule_desc):
    with db_cursor() as cursor:
        # this will fetch the current known constraints for the tool.
        cursor.execute("SELECT known_constraints FROM capabilities WHERE op_name = ?", (tool_name,))
        db_result = cursor.fetchone()
        
        if not db_result:
            return
            
        current_rules = []
        if db_result["known_constraints"]:
            current_rules = json.loads(db_result["known_constraints"])
            
        # Check if we already have this rule so we don't add duplicates
        for rule in current_rules:
            if rule.get("type") == rule_kind and rule.get("detail") == rule_desc:
                return
                
        current_rules.append({
            "type": rule_kind, 
            "detail": rule_desc, 
            "learned_at": get_current_time_str()
        })
        
        cursor.execute(
            "UPDATE capabilities SET known_constraints = ? WHERE op_name = ?",
            (json.dumps(current_rules), tool_name),
        )


def fetch_rules_for_multiple_tools(tool_names_list):
    if len(tool_names_list) == 0:
        return {}
        
    question_marks = ",".join(["?"] * len(tool_names_list))
    
    with db_cursor() as cursor:
        # this will fetch the known constraints for multiple tools at once.
        cursor.execute(
            f"SELECT op_name, known_constraints FROM capabilities WHERE op_name IN ({question_marks})",
            tool_names_list,
        )
        results = cursor.fetchall()
        
        rules_dict = {}
        for row in results:
            parsed = []
            if row["known_constraints"]:
                parsed = json.loads(row["known_constraints"])
            rules_dict[row["op_name"]] = parsed
            
        return rules_dict