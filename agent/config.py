import os

from dotenv import load_dotenv

load_dotenv()

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "")

DB_PATH = os.environ.get("AGENT_DB_PATH", "data/agent_memory.sqlite")

# "subprocess" (default) runs synthesized code in a resource-limited child process;
# "inprocess" execs it in the agent process (used by the offline demo harness).
SANDBOX_MODE = os.environ.get("AGENT_SANDBOX_MODE", "subprocess")

DEFAULT_STALE_DAYS = 14
AVOID_OP_FAILURE_RATE = 0.5
