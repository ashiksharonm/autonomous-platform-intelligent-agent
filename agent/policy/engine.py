"""Evaluate a planned list of steps against a Policy, before anything is executed."""

from dataclasses import dataclass, field

from agent.policy.model import Policy

ALLOW, APPROVAL, DENY = "allow", "needs_approval", "deny"


@dataclass
class StepDecision:
    index: int
    op_name: str
    params: dict
    is_write: bool
    verdict: str = ALLOW
    reasons: list = field(default_factory=list)

    def flag(self, verdict, reason):
        # deny beats needs_approval beats allow
        order = {ALLOW: 0, APPROVAL: 1, DENY: 2}
        if order[verdict] > order[self.verdict]:
            self.verdict = verdict
        self.reasons.append(reason)


@dataclass
class PlanDecision:
    repo: str
    verdict: str
    steps: list
    blast_radius: int
    max_blast_radius: int
    plan_reasons: list = field(default_factory=list)

    @property
    def allowed(self):
        return self.verdict != DENY

    @property
    def needs_approval(self):
        return self.verdict == APPROVAL

    def to_dict(self):
        return {
            "repo": self.repo,
            "verdict": self.verdict,
            "blast_radius": self.blast_radius,
            "max_blast_radius": self.max_blast_radius,
            "plan_reasons": self.plan_reasons,
            "steps": [
                {"index": s.index, "op": s.op_name, "write": s.is_write, "verdict": s.verdict, "reasons": s.reasons}
                for s in self.steps
            ],
        }


def evaluate_plan(policy: Policy, repo: str, steps, synthesized_ops=frozenset()) -> PlanDecision:
    """Decide allow / needs_approval / deny for a whole plan.

    `synthesized_ops` are op names backed by LLM-written code from capability memory;
    NEEDS_SYNTHESIS steps are treated the same way. The decision is made on the plan,
    so a denied plan executes nothing, not even its read-only steps.
    """
    decisions = []
    plan_reasons = []
    if not policy.repo_allowed(repo):
        plan_reasons.append(f"repo '{repo}' is not in allowed_repos")

    writes = 0
    for index, step in enumerate(steps):
        op = step.op_name
        synthesized = op == "NEEDS_SYNTHESIS" or op in synthesized_ops
        d = StepDecision(index=index, op_name=op, params=step.params, is_write=policy.is_write(op))
        if d.is_write:
            writes += 1
        if synthesized:
            mode = policy.synthesized_code
            if mode == "deny":
                d.flag(DENY, "LLM-synthesized code is denied by policy")
            elif mode == "require_approval":
                d.flag(APPROVAL, "LLM-synthesized code requires human approval")
        elif op not in policy.allowed_ops:
            d.flag(DENY, f"op '{op}' is not in allowed_ops")
        if op in policy.require_approval:
            d.flag(APPROVAL, f"op '{op}' requires human approval")
        decisions.append(d)

    if writes > policy.max_blast_radius:
        plan_reasons.append(f"{writes} write steps exceed max_blast_radius={policy.max_blast_radius}")
    overall = ALLOW
    if plan_reasons or any(d.verdict == DENY for d in decisions):
        overall = DENY
    elif any(d.verdict == APPROVAL for d in decisions):
        overall = APPROVAL
    elif policy.approve_above is not None and writes > policy.approve_above:
        overall = APPROVAL
        plan_reasons.append(f"{writes} write steps exceed approve_above={policy.approve_above}")
    return PlanDecision(
        repo=repo,
        verdict=overall,
        steps=decisions,
        blast_radius=writes,
        max_blast_radius=policy.max_blast_radius,
        plan_reasons=plan_reasons,
    )


def render_plan(request_id: str, instruction: str, decision: PlanDecision) -> str:
    """Human-readable dry-run plan: what would run, what the policy says about each step."""
    lines = [
        f"Request {request_id}",
        f"Instruction: {instruction}",
        f"Target repo: {decision.repo}",
        f"Policy verdict: {decision.verdict.upper()}  (write steps: {decision.blast_radius}/{decision.max_blast_radius})",
    ]
    lines += [f"  ! {reason}" for reason in decision.plan_reasons]
    lines.append("")
    lines.append("Planned steps:")
    for s in decision.steps:
        kind = "write" if s.is_write else "read "
        lines.append(f"  [{s.index}] {kind} {s.op_name} {s.params}  -> {s.verdict}")
        lines += [f"        - {reason}" for reason in s.reasons]
    return "\n".join(lines)
