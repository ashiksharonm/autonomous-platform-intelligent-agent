"""Child-process entry point for running synthesized code (see agent/sandbox.py).

Reads {"source", "params", "repo_mode"} as JSON on stdin and writes one JSON line
{"ok": bool, "result"|"error": ...} to stdout. Not meant to be invoked by hand.
"""

import json
import os
import sys

MB = 1024 * 1024


def _apply_rlimits(cpu_seconds):
    try:
        import resource
    except ImportError:  # non-POSIX
        return
    limits = [
        (resource.RLIMIT_CPU, int(cpu_seconds) + 1),
        (resource.RLIMIT_FSIZE, 1 * MB),
        (resource.RLIMIT_NOFILE, 64),
    ]
    if sys.platform.startswith("linux"):
        # RLIMIT_AS is not enforceable on macOS; RLIMIT_NPROC counts threads on Linux,
        # which PyGithub/urllib3 do not need.
        limits.append((resource.RLIMIT_AS, 1024 * MB))
        limits.append((resource.RLIMIT_NPROC, 0))
    for which, value in limits:
        try:
            _, hard = resource.getrlimit(which)
            if hard != resource.RLIM_INFINITY:
                value = min(value, hard)
            resource.setrlimit(which, (value, hard))
        except (ValueError, OSError):
            pass


def _build_repo(repo_mode):
    if repo_mode == "none":
        return None
    from github import Auth, Github

    client = Github(auth=Auth.Token(os.environ["GITHUB_TOKEN"]))
    return client.get_repo(os.environ["GITHUB_REPO"])


def main():
    out = sys.stdout
    sys.stdout = sys.stderr  # nothing the synthesized code does may corrupt the reply line
    try:
        request = json.loads(sys.stdin.read())
        from agent.sandbox import load_run_function

        repo = _build_repo(request.get("repo_mode", "github"))
        run = load_run_function(request["source"])
        _apply_rlimits(float(os.environ.get("AGENT_SANDBOX_TIMEOUT_S", "30")))
        result = run(repo, request.get("params") or {})
        reply = {"ok": True, "result": json.loads(json.dumps(result, default=str))}
    except BaseException as exc:  # noqa: BLE001 - everything is reported to the parent
        reply = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:500]}
    out.write(json.dumps(reply) + "\n")
    out.flush()


if __name__ == "__main__":
    main()
