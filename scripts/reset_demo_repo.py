import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.config import target_repo_name
from agent.github_tools import create_gh_session

def close_all_active_bugs():
    github_connection = create_gh_session()
    target_repo = github_connection.get_repo(target_repo_name)
    
    found_issues = target_repo.get_issues(state="open")
    
    issues_list = []
    for item in found_issues:
        issues_list.append(item)
        
    if len(issues_list) == 0:
        print("No open issues to close.")
    else:
        for single_issue in issues_list:
            single_issue.edit(state="closed")
            print("Closed #" + str(single_issue.number) + ": " + single_issue.title)

if __name__ == "__main__":
    close_all_active_bugs()