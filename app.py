"""Gradio demo (Hugging Face Space entrypoint): the policy gate over a mock GitHub, no tokens needed.

    python app.py            # http://localhost:7860, /healthz for probes
"""

import os

# The hosted demo never touches real GitHub; LLM defaults to the deterministic mock planner
# but can be pointed at a free-tier provider locally (LLM_PROVIDER=gemini|groq + key).
os.environ["GITHUB_MODE"] = "mock"
os.environ.setdefault("LLM_PROVIDER", "mock")

import gradio as gr  # noqa: E402
from fastapi import FastAPI  # noqa: E402

from agent.demo_service import SAMPLES, DemoService  # noqa: E402

service = DemoService()

STATUS_TEXT = {
    "executed": "Executed after passing the policy gate.",
    "blocked_by_policy": "BLOCKED by policy. Nothing was executed.",
    "dry_run": "Dry run. Nothing was executed.",
    "awaiting_approval": "Waiting for a human decision. Nothing has been executed yet.",
    "rejected_by_human": "Rejected by the approver. Nothing was executed.",
    "error": "Could not process the request.",
}


def _view(result):
    status = result["status"]
    show = status == "awaiting_approval"
    return (
        STATUS_TEXT[status],
        result.get("plan", ""),
        result.get("report", ""),
        result.get("request_id"),
        gr.update(visible=show),
        gr.update(visible=show),
        service.audit_text(),
        service.repo_text(),
    )


def on_submit(instruction, policy_text, dry_run):
    return _view(service.submit(instruction, policy_text, dry_run))


def on_decide(request_id, approve):
    return _view(service.decide(request_id, approve))


def on_reset():
    service.reset()
    return "Demo state reset.", "", "", None, gr.update(visible=False), gr.update(visible=False), service.audit_text(), service.repo_text()


with gr.Blocks(title="Policy-gated GitHub agent") as demo:
    gr.Markdown(
        "# Policy-gated GitHub agent\n"
        "Natural-language instruction -> plan -> **YAML policy gate** -> (human approval) -> execute, with a "
        "hash-chained audit log. Runs against an **in-memory mock GitHub** with a **keyword planner (not an LLM)**, "
        "so no token is needed and nothing real can be changed. Edit the policy and watch the verdicts change."
    )
    with gr.Row():
        with gr.Column(scale=3):
            instruction = gr.Textbox(label="Instruction", lines=2, placeholder="Create an issue titled 'Fix login bug' with label 'bug'")
            gr.Examples(examples=[[text] for _, text in SAMPLES], inputs=[instruction], label="Sample prompts (" + "; ".join(name for name, _ in SAMPLES) + ")")
            dry_run = gr.Checkbox(label="Dry run (plan and evaluate only)")
            go = gr.Button("Plan and run through the gate", variant="primary")
            status = gr.Markdown()
            plan = gr.Code(label="Plan and policy verdict", language=None, interactive=False)
            with gr.Row():
                approve = gr.Button("Approve and execute", visible=False, variant="primary")
                reject = gr.Button("Reject", visible=False)
            report = gr.Code(label="Execution report", language=None, interactive=False)
        with gr.Column(scale=2):
            policy_text = gr.Code(label="policy.yaml (editable)", language="yaml", value=service.default_policy)
            repo = gr.Code(label="Mock repo state", language=None, interactive=False, value=service.repo_text())
            audit = gr.Code(label="Audit log (append-only, hash-chained)", language=None, interactive=False, value=service.audit_text())
            reset = gr.Button("Reset demo state")
    pending = gr.State(None)
    outputs = [status, plan, report, pending, approve, reject, audit, repo]
    go.click(on_submit, [instruction, policy_text, dry_run], outputs)
    approve.click(lambda rid: on_decide(rid, True), [pending], outputs)
    reject.click(lambda rid: on_decide(rid, False), [pending], outputs)
    reset.click(on_reset, None, outputs)

app = FastAPI()


@app.get("/healthz")
def healthz():
    return {"ok": True}


app = gr.mount_gradio_app(app, demo, path="/")

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "7860")))  # noqa: S104 (container entrypoint)
