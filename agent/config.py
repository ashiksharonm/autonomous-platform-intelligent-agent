import os

from dotenv import load_dotenv

load_dotenv()

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")

# LLM provider: "auto" picks groq (GROQ_API_KEY) > gemini (GEMINI_API_KEY) > mock.
# groq and gemini both have free tiers; mock is a deterministic keyword planner, not a model.
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "auto")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

# "live" talks to GitHub; "mock" serves an in-memory fake repo and needs no token.
GITHUB_MODE = os.environ.get("GITHUB_MODE", "live")
MOCK_REPO = "demo/sandbox-repo"

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "")

DB_PATH = os.environ.get("AGENT_DB_PATH", "data/agent_memory.sqlite")

# "subprocess" (default) runs synthesized code in a resource-limited child process;
# "inprocess" execs it in the agent process (used by the offline demo harness).
SANDBOX_MODE = os.environ.get("AGENT_SANDBOX_MODE", "subprocess")

DEFAULT_STALE_DAYS = 14
AVOID_OP_FAILURE_RATE = 0.5


def effective_repo():
    """The repo the agent will act on (the mock repo name in mock mode)."""
    return MOCK_REPO if GITHUB_MODE == "mock" and not GITHUB_REPO else GITHUB_REPO
