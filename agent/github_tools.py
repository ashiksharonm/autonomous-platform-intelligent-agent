from datetime import datetime, timezone

from github import Github, GithubException

from agent.config import DEFAULT_STALE_DAYS, GITHUB_REPO, GITHUB_TOKEN

active_gh_session = None


def create_gh_session():
    global active_gh_session
    if active_gh_session is None:
        active_gh_session = Github(GITHUB_TOKEN)
    return active_gh_session


def fetch_my_repo():
    session = create_gh_session()
    return session.get_repo(GITHUB_REPO)


def calls_remaining():
    session = create_gh_session()
    rate_info = session.get_rate_limit()
    return rate_info.core.remaining


def count_days_passed(date_obj):
    if date_obj.tzinfo is None:
        date_obj = date_obj.replace(tzinfo=timezone.utc)
        
    time_now = datetime.now(timezone.utc)
    diff = time_now - date_obj
    return diff.days


def make_new_issue(target_repo, args_dict):
    issue_title = args_dict["title"]
    
    issue_body = ""
    if "body" in args_dict:
        issue_body = args_dict["body"]
        
    issue_tags = []
    if "labels" in args_dict:
        issue_tags = args_dict["labels"]
        
    new_item = target_repo.create_issue(title=issue_title, body=issue_body, labels=issue_tags)
    
    return {"issue_url": new_item.html_url, "issue_number": new_item.number}


def verify_or_create_tag(target_repo, args_dict):
    tag_name = args_dict["name"]
    
    tag_color = "ededed"
    if "color" in args_dict:
        tag_color = args_dict["color"]
        
    try:
        target_repo.get_label(tag_name)
        return {"label": tag_name, "created": False}
    except GithubException as g_error:
        if g_error.status != 404:
            raise
        target_repo.create_label(name=tag_name, color=tag_color)
        return {"label": tag_name, "created": True}


def attach_tags_to_issue(target_repo, args_dict):
    target_issue = target_repo.get_issue(args_dict["issue_number"])
    tags_to_add = args_dict["labels"]
    
    target_issue.add_to_labels(*tags_to_add)
    
    return {"issue_number": target_issue.number, "labels_added": tags_to_add}


def write_issue_comment(target_repo, args_dict):
    target_issue = target_repo.get_issue(args_dict["issue_number"])
    msg_text = args_dict["body"]
    
    new_comment = target_issue.create_comment(msg_text)
    
    return {"comment_id": new_comment.id, "issue_number": target_issue.number}


def locate_old_issues(target_repo, args_dict):
    target_tag = args_dict["label"]
    
    cutoff_days = DEFAULT_STALE_DAYS
    if "stale_days" in args_dict:
        cutoff_days = args_dict["stale_days"]
        
    found_issues = target_repo.get_issues(state="open", labels=[target_tag])
    
    old_issue_ids = []
    for issue_item in found_issues:
        days_inactive = count_days_passed(issue_item.updated_at)
        
        if days_inactive >= cutoff_days:
            old_issue_ids.append(issue_item.number)
            
    return {"stale_issue_numbers": old_issue_ids}


TOOL_REGISTRY = {
    "create_issue": {
        "fn": make_new_issue,
        "signature_desc": "Create a new issue. params: {title: str, body?: str, labels?: list[str]}",
    },
    "ensure_label_exists": {
        "fn": verify_or_create_tag,
        "signature_desc": "Create a label if it doesn't already exist. params: {name: str, color?: str}",
    },
    "add_labels_to_issue": {
        "fn": attach_tags_to_issue,
        "signature_desc": "Add labels to an existing issue by number. params: {issue_number: int, labels: list[str]}",
    },
    "comment_on_issue": {
        "fn": write_issue_comment,
        "signature_desc": "Post a comment on an issue by number. params: {issue_number: int, body: str}",
    },
    "find_stale_labeled_issues": {
        "fn": locate_old_issues,
        "signature_desc": "Find open issue numbers with a given label that haven't been updated in stale_days (default 14). params: {label: str, stale_days?: int}",
    },
}


def fetch_tool_function(tool_key_name):
    if tool_key_name in TOOL_REGISTRY:
        return TOOL_REGISTRY[tool_key_name]["fn"]
    return None