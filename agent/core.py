import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from agent import config, decomposer, executor, report
from agent.github_tools import TOOL_REGISTRY
from agent.log import log_event
from agent.memory import capability_memory, execution_memory
from agent.memory.db import setup_tables
from agent.models import FinalReport
from agent.policy import PlanDecision, evaluate_plan, render_plan

# Outcomes produced by the policy gate (no step executed).
BLOCKED, REJECTED, DRY_RUN = "blocked_by_policy", "rejected_by_human", "dry_run"


def get_time_now():
    return datetime.now(timezone.utc).isoformat()


def load_default_tools():
    for tool_key, tool_info in TOOL_REGISTRY.items():
        capability_memory.ensure_builtin(tool_key, tool_info["signature_desc"])


def new_request_id():
    return "req_" + uuid.uuid4().hex[:12]


@dataclass
class PlannedJob:
    request_id: str
    instruction: str
    task_type: str
    steps: list
    decision: PlanDecision | None = None  # None when no policy is active

    @property
    def plan_text(self):
        if self.decision:
            return render_plan(self.request_id, self.instruction, self.decision)
        lines = [f"Request {self.request_id} (no policy active)", f"Instruction: {self.instruction}", "Planned steps:"]
        return "\n".join(lines + [f"  [{i}] {s.op_name} {s.params}" for i, s in enumerate(self.steps)])


def plan_job(user_command, *, policy=None, audit=None, request_id=None):
    """Decompose the instruction and evaluate the plan against the policy. Executes nothing."""
    setup_tables()
    load_default_tools()
    request_id = request_id or new_request_id()
    if audit:
        audit.append(request_id, "request", instruction=user_command, repo=config.effective_repo())

    job_category, plan_steps = decomposer.break_down_command(user_command)
    safe_steps = executor.apply_memory_guard(job_category, plan_steps)

    decision = None
    if policy is not None:
        synthesized = {t["op_name"] for t in capability_memory.list_all() if t["source"] == "synthesized"}
        decision = evaluate_plan(policy, config.effective_repo(), safe_steps, synthesized)
    job = PlannedJob(request_id, user_command, job_category, safe_steps, decision)
    if audit:
        audit.append(
            request_id,
            "plan",
            task_type=job_category,
            steps=[{"op": s.op_name, "params": s.params} for s in safe_steps],
            policy=policy.source if policy else None,
            decision=decision.to_dict() if decision else None,
        )
    log_event("planned", request_id=request_id, task_type=job_category, steps=len(safe_steps), verdict=decision.verdict if decision else "no_policy")
    return job


def execute_job(job, *, audit=None, approved_by=None):
    """Run a planned job's steps and record them. The caller is responsible for the gate."""
    current_job_id = str(uuid.uuid4())
    time_started = get_time_now()
    safe_steps = job.steps

    parsed_plan = [{"op_name": s.op_name, "params": s.params, "blocking": s.blocking} for s in safe_steps]
    execution_memory.create_job_entry(
        run_id=current_job_id,
        raw_instruction=job.instruction,
        task_type=job.task_type,
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
        if audit:
            audit.append(
                job.request_id,
                "step",
                run_id=current_job_id,
                index=step_idx,
                op=single_step.op_name,
                status=step_out.status,
                api_calls=step_out.api_calls,
                error_class=step_out.error_class,
            )

        if step_out.status == "failed" and single_step.blocking:
            break

    time_ended = get_time_now()
    final_summary = report.build(current_job_id, job.instruction, job.task_type, safe_steps, all_results)
    final_summary.request_id = job.request_id
    final_summary.plan_text = job.plan_text

    execution_memory.finalize_run(
        run_id=current_job_id,
        outcome=final_summary.outcome,
        total_api_calls=final_summary.total_api_calls,
        total_latency_ms=final_summary.total_latency_ms,
        finished_at=time_ended,
    )
    if audit:
        audit.append(job.request_id, "outcome", run_id=current_job_id, outcome=final_summary.outcome, approved_by=approved_by)
    log_event("finished", request_id=job.request_id, outcome=final_summary.outcome, api_calls=final_summary.total_api_calls)
    return final_summary


def _gate_report(job, outcome):
    return FinalReport(
        run_id="-",
        instruction=job.instruction,
        task_type=job.task_type,
        outcome=outcome,
        steps=job.steps,
        step_results=[],
        total_api_calls=0,
        total_latency_ms=0,
        request_id=job.request_id,
        plan_text=job.plan_text,
    )


AWAITING_APPROVAL, RUN = "awaiting_approval", "run"


def gate(job, *, dry_run=False, audit=None):
    """Decide what happens to a planned job: BLOCKED, DRY_RUN, AWAITING_APPROVAL or RUN."""
    decision = job.decision
    if decision is not None and not decision.allowed:
        if audit:
            audit.append(job.request_id, "blocked", reasons=decision.plan_reasons)
        return BLOCKED
    if dry_run:
        if audit:
            audit.append(job.request_id, "dry_run")
        return DRY_RUN
    if decision is not None and decision.needs_approval:
        return AWAITING_APPROVAL
    return RUN


def record_approval(job, approved, by, audit=None):
    """Audit the human decision; returns who approved (None when rejected)."""
    who = by if approved else None
    if audit:
        audit.append(job.request_id, "approval", approved=bool(approved), by=who)
    return who


def start_agent_job(user_command, *, policy=None, approver=None, dry_run=False, audit=None, request_id=None):
    """Plan, gate, and (if permitted) execute an instruction.

    Without a policy this behaves exactly as before. With one:
      deny           -> nothing executes, outcome "blocked_by_policy"
      dry_run        -> nothing executes, outcome "dry_run" (the plan is in the report)
      needs_approval -> `approver(job)` must return True, otherwise "rejected_by_human"
                        (no approver means reject: the gate fails closed)
    """
    job = plan_job(user_command, policy=policy, audit=audit, request_id=request_id)
    action = gate(job, dry_run=dry_run, audit=audit)
    if action in (BLOCKED, DRY_RUN):
        return _gate_report(job, action)
    approved_by = None
    if action == AWAITING_APPROVAL:
        approved = bool(approver(job)) if approver else False
        approved_by = record_approval(job, approved, getattr(approver, "name", "human"), audit)
        if not approved:
            return _gate_report(job, REJECTED)
    return execute_job(job, audit=audit, approved_by=approved_by)
