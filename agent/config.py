import os

from dotenv import load_dotenv

load_dotenv()

my_groq_key = os.environ.get("GROQ_API_KEY", "")
ai_model_name = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")

git_pass_token = os.environ.get("GITHUB_TOKEN", "")
target_repo_name = os.environ.get("GITHUB_REPO", "")

sqlite_file_location = os.environ.get("AGENT_DB_PATH", "data/agent_memory.sqlite")

max_old_days = 14
max_fail_percent = 0.5