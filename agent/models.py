from dataclasses import dataclass, field


@dataclass
class JobStep:
    op_name: str
    params: dict
    blocking: bool = True
    capability_request: str = None
    requested_op_name: str = None
    param_schema: dict = None


@dataclass
class StepOutcome:
    step: JobStep
    status: str
    api_calls: int = 0
    latency_ms: int = 0
    error_class: str = None
    error_message: str = None
    agent_decision: str = None
    artifacts: dict = field(default_factory=dict)


@dataclass
class FinalReport:
    run_id: str
    instruction: str
    task_type: str
    outcome: str
    steps: list
    step_results: list
    total_api_calls: int
    total_latency_ms: int

    def to_text(self):
        text_lines = []
        text_lines.append("Run " + self.run_id + " — task_type=" + self.task_type)
        text_lines.append("Instruction: " + self.instruction)
        text_lines.append("Outcome: " + self.outcome)
        text_lines.append("Total API calls: " + str(self.total_api_calls) + "  |  Total latency: " + str(self.total_latency_ms) + "ms")
        text_lines.append("")
        text_lines.append("Steps:")

        for idx, step_out in enumerate(self.step_results):
            line_str = "  [" + str(idx) + "] " + step_out.step.op_name + " -> " + step_out.status
            line_str = line_str + " (api_calls=" + str(step_out.api_calls) + ", latency_ms=" + str(step_out.latency_ms) + ")"
            text_lines.append(line_str)

            if step_out.artifacts:
                for k, v in step_out.artifacts.items():
                    text_lines.append("      artifact." + str(k) + " = " + str(v))

            if step_out.status == "failed":
                text_lines.append("      error_class=" + str(step_out.error_class) + ": " + str(step_out.error_message))

            if step_out.agent_decision:
                text_lines.append("      agent_decision: " + str(step_out.agent_decision))

        return "\n".join(text_lines)
