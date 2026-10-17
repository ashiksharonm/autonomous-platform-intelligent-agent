"""Policy-as-code: a YAML file that decides what the agent may write.

Example (see policies/default.yaml):

    version: 1
    allowed_repos: ["octo/*", "demo/sandbox-repo"]   # fnmatch patterns
    allowed_ops: [create_issue, comment_on_issue]    # anything else is denied
    read_only_ops: [find_stale_labeled_issues]       # not counted as writes
    max_blast_radius: 5                              # max write steps in one plan
    approve_above: 3                                 # more writes than this needs a human
    require_approval: [comment_on_issue]             # ops that always need a human
    synthesized_code: require_approval               # deny | require_approval | allow
"""

from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path

import yaml

BUILTIN_OPS = (
    "create_issue",
    "ensure_label_exists",
    "add_labels_to_issue",
    "comment_on_issue",
    "find_stale_labeled_issues",
)
SYNTH_MODES = ("deny", "require_approval", "allow")
KNOWN_KEYS = {
    "version",
    "allowed_repos",
    "allowed_ops",
    "read_only_ops",
    "max_blast_radius",
    "approve_above",
    "require_approval",
    "synthesized_code",
}


class PolicyError(ValueError):
    """The policy file is malformed. The agent refuses to run rather than guess."""


@dataclass(frozen=True)
class Policy:
    allowed_repos: tuple = ()
    allowed_ops: tuple = BUILTIN_OPS
    read_only_ops: tuple = ("find_stale_labeled_issues",)
    max_blast_radius: int = 5
    approve_above: int | None = None
    require_approval: tuple = ()
    synthesized_code: str = "require_approval"
    source: str = field(default="<inline>", compare=False)

    def repo_allowed(self, repo: str) -> bool:
        return any(fnmatchcase(repo, pattern) for pattern in self.allowed_repos)

    def is_write(self, op_name: str) -> bool:
        # Unknown ops (including NEEDS_SYNTHESIS) count as writes: fail closed.
        return op_name not in self.read_only_ops


def _str_list(raw, key):
    if not isinstance(raw, list) or not all(isinstance(item, str) and item for item in raw):
        raise PolicyError(f"'{key}' must be a list of non-empty strings")
    return tuple(raw)


def _int(raw, key, minimum):
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < minimum:
        raise PolicyError(f"'{key}' must be an integer >= {minimum}")
    return raw


def parse_policy(text: str, source: str = "<inline>") -> Policy:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise PolicyError(f"invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise PolicyError("policy must be a YAML mapping")
    unknown = set(data) - KNOWN_KEYS
    if unknown:
        raise PolicyError(f"unknown policy keys: {sorted(unknown)}")
    if data.get("version", 1) != 1:
        raise PolicyError("unsupported policy version (expected 1)")
    if "allowed_repos" not in data:
        raise PolicyError("'allowed_repos' is required (an empty policy would allow nothing)")

    kwargs: dict = {"allowed_repos": _str_list(data["allowed_repos"], "allowed_repos"), "source": source}
    for key in ("allowed_ops", "read_only_ops", "require_approval"):
        if key in data:
            kwargs[key] = _str_list(data[key], key)
    if "max_blast_radius" in data:
        kwargs["max_blast_radius"] = _int(data["max_blast_radius"], "max_blast_radius", 0)
    if "approve_above" in data:
        kwargs["approve_above"] = _int(data["approve_above"], "approve_above", 0)
    if "synthesized_code" in data:
        if data["synthesized_code"] not in SYNTH_MODES:
            raise PolicyError(f"'synthesized_code' must be one of {SYNTH_MODES}")
        kwargs["synthesized_code"] = data["synthesized_code"]
    return Policy(**kwargs)


def load_policy(path) -> Policy:
    path = Path(path)
    try:
        text = path.read_text()
    except OSError as exc:
        raise PolicyError(f"cannot read policy {path}: {exc}") from exc
    return parse_policy(text, source=str(path))
