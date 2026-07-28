# GitHub Autonomous Platform Agent

Takes a natural-language instruction, decomposes it into GitHub API operations, executes them for real, and persists what it learns so later runs get cheaper and more reliable. See `ARCHITECTURE.md` for how memory, capability synthesis, and the learning signal actually work.

## Setup

```
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Fill in `.env`:
- `GROQ_API_KEY` — from console.groq.com
- `GITHUB_TOKEN` — a PAT with issue read/write scope on a scratch repo you own
- `GITHUB_REPO` — `owner/repo` of that scratch repo

## Run

```
python main.py run "Create an issue titled 'Fix login bug' with label 'needs-triage' on the repo"
python main.py stats create_labeled_issue
python main.py show-capabilities
```

## Tests

```
pytest tests/
```

Tests cover the memory layer (capability stats, constraint dedup, avoid_ops/preferred_decomposition derivation), the synthesis AST safety checker, and decomposer JSON parsing against fixture LLM responses — no live API calls.

## Notes

- Memory lives in `data/agent_memory.sqlite` and is never wiped automatically — that's required for the learning signal to be visible across runs. Delete it manually only if you want a clean slate.
- `scripts/reset_demo_repo.py` is a demo-only cleanup utility (closes issues created during a take) — it's not part of the agent and isn't imported by anything under `agent/`.
