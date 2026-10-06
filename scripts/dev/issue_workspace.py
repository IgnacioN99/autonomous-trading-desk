#!/usr/bin/env python3
"""
scripts/dev/issue_workspace.py - Deterministic helpers for the issue-orchestrator skill (development work only;
it never touches the exchange, the ledger or any desk runtime file).

  init <N> --slug <slug> [--base origin/main]
      git fetch origin; create the sibling worktree <repo>-wt-issue-<N> on a new branch fix/issue-<N>-<slug> from
      <base>; dump the open issue (gh issue view) to <worktree>/logs/issue_work/issue.json.
  review-context <worktree>
      Write the auditor's read-only input to <worktree>/logs/issue_work/review/:
        diff.patch   uncommitted changes vs HEAD plus every untracked file as a new-file diff
        files.txt    changed and new files
        checks.json  compileall, sync_claude_assets --check and unittest discover: exit code, duration, summary
        checks.log   the tail of each check's output
  cleanup <N> [--force]
      Once the PR of fix/issue-<N>-* is merged (or with --force), remove the worktree and delete the local branch.
      It never deletes remote branches (GitHub deletes merged head branches, and a push would re-arm the
      pr-review hook).

Every command prints one JSON document. Exit codes: 0 ok (review-context also exits 0 when checks fail: read
`checks_ok`), 1 failure, 2 invalid arguments.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time

WORK_DIR = os.path.join("logs", "issue_work")
REVIEW_DIR = os.path.join(WORK_DIR, "review")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
CHECK_TIMEOUT_SECONDS = 1800
LOG_TAIL_LINES = 120


def _checks(python: str) -> list:
    return [
        ("compileall", [python, "-m", "compileall", "-q", "scripts/", "tests/"]),
        ("sync_claude_assets", [python, "scripts/dev/sync_claude_assets.py", "--check"]),
        ("unittest", [python, "-m", "unittest", "discover", "tests/"]),
    ]


class WorkspaceError(Exception):
    pass


def run(cmd: list, cwd: str = None, check: bool = True, timeout: int = 300) -> subprocess.CompletedProcess:
    res = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if check and res.returncode != 0:
        raise WorkspaceError(f"{' '.join(cmd)} failed (exit {res.returncode}): {(res.stderr or res.stdout).strip()}")
    return res


def main_repo_root(cwd: str = None) -> str:
    """Root of the MAIN checkout, also when called from inside a linked worktree."""
    common = run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=cwd).stdout.strip()
    return os.path.dirname(common.rstrip("/"))


def worktree_path(repo_root: str, issue: int) -> str:
    return os.path.join(os.path.dirname(repo_root), f"{os.path.basename(repo_root)}-wt-issue-{issue}")


# --------------------------------------------------------------------------------------------------
# init
# --------------------------------------------------------------------------------------------------
def cmd_init(issue: int, slug: str, base: str, cwd: str = None) -> dict:
    if not SLUG_RE.match(slug):
        raise WorkspaceError(f"invalid slug {slug!r}: use 1-40 lowercase letters, digits and dashes")
    repo = main_repo_root(cwd)
    view = run(["gh", "issue", "view", str(issue), "--json", "number,title,body,labels,state,url"], cwd=repo)
    data = json.loads(view.stdout)
    if str(data.get("state", "")).upper() != "OPEN":
        raise WorkspaceError(f"issue #{issue} is {data.get('state')}, not OPEN")
    path = worktree_path(repo, issue)
    branch = f"fix/issue-{issue}-{slug}"
    if os.path.exists(path):
        raise WorkspaceError(f"{path} already exists (another session may be working on #{issue})")
    if run(["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], cwd=repo, check=False).returncode == 0:
        raise WorkspaceError(f"branch {branch} already exists")
    run(["git", "fetch", "--quiet", "origin"], cwd=repo)
    run(["git", "worktree", "add", "--quiet", path, "-b", branch, base], cwd=repo)
    work = os.path.join(path, WORK_DIR)
    os.makedirs(work, exist_ok=True)
    with open(os.path.join(work, "issue.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    head = run(["git", "rev-parse", "--short", "HEAD"], cwd=path).stdout.strip()
    return {"ok": True, "issue": issue, "title": data.get("title"), "worktree": path, "branch": branch,
            "base": base, "head": head, "work_dir": work, "issue_file": os.path.join(work, "issue.json")}


# --------------------------------------------------------------------------------------------------
# review-context
# --------------------------------------------------------------------------------------------------
def _untracked(path: str) -> list:
    out = run(["git", "ls-files", "--others", "--exclude-standard", "-z"], cwd=path).stdout
    return sorted(p for p in out.split("\0") if p)


def _summary(name: str, output: str) -> str:
    lines = [ln.strip() for ln in output.splitlines() if ln.strip()]
    if name == "unittest":
        ran = next((ln for ln in reversed(lines) if ln.startswith("Ran ")), "")
        verdict = next((ln for ln in reversed(lines) if ln.startswith(("OK", "FAILED"))), "")
        return " | ".join(x for x in (ran, verdict) if x)
    return lines[-1] if lines else ""


def cmd_review_context(path: str, python: str = None) -> dict:
    path = os.path.abspath(path)
    if run(["git", "rev-parse", "--is-inside-work-tree"], cwd=path, check=False).stdout.strip() != "true":
        raise WorkspaceError(f"{path} is not a git worktree")
    review = os.path.join(path, REVIEW_DIR)
    os.makedirs(review, exist_ok=True)

    tracked = run(["git", "diff", "HEAD", "--no-color"], cwd=path).stdout
    names = [ln for ln in run(["git", "diff", "HEAD", "--name-status"], cwd=path).stdout.splitlines() if ln]
    parts = [tracked]
    new_files = _untracked(path)
    for rel in new_files:
        parts.append(run(["git", "diff", "--no-color", "--no-index", "--", os.devnull, rel], cwd=path,
                         check=False).stdout)
    with open(os.path.join(review, "diff.patch"), "w", encoding="utf-8") as f:
        f.write("".join(parts))
    with open(os.path.join(review, "files.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(names + [f"A\t{rel}  (untracked)" for rel in new_files]) + "\n")

    results, log_parts = [], []
    for name, cmd in _checks(python or sys.executable):
        start = time.time()
        try:
            res = subprocess.run(cmd, cwd=path, capture_output=True, text=True, timeout=CHECK_TIMEOUT_SECONDS)
            code, output = res.returncode, (res.stdout or "") + (res.stderr or "")
        except subprocess.TimeoutExpired as e:
            code, output = 124, f"TIMEOUT after {CHECK_TIMEOUT_SECONDS}s\n{e.stdout or ''}{e.stderr or ''}"
        results.append({"name": name, "command": " ".join(cmd[1:]), "exit_code": code, "ok": code == 0,
                        "duration_s": round(time.time() - start, 1), "summary": _summary(name, output)})
        tail = "\n".join(output.splitlines()[-LOG_TAIL_LINES:])
        log_parts.append(f"===== {name} (exit {code}) =====\n{tail}\n")
    with open(os.path.join(review, "checks.log"), "w", encoding="utf-8") as f:
        f.write("\n".join(log_parts))
    checks_ok = all(r["ok"] for r in results)
    with open(os.path.join(review, "checks.json"), "w", encoding="utf-8") as f:
        json.dump({"generated_at_ts": int(time.time()), "checks_ok": checks_ok, "checks": results}, f, indent=2)
    return {"ok": True, "worktree": path, "review_dir": review, "changed_files": len(names) + len(new_files),
            "untracked_files": new_files, "checks_ok": checks_ok,
            "checks": {r["name"]: r["summary"] or f"exit {r['exit_code']}" for r in results}}


# --------------------------------------------------------------------------------------------------
# cleanup
# --------------------------------------------------------------------------------------------------
def _worktree_branch(repo: str, path: str) -> str:
    out = run(["git", "worktree", "list", "--porcelain"], cwd=repo).stdout
    current = None
    for line in out.splitlines():
        if line.startswith("worktree "):
            current = os.path.normpath(line[len("worktree "):])
        elif line.startswith("branch ") and current == os.path.normpath(path):
            return line[len("branch refs/heads/"):]
    return ""


def cmd_cleanup(issue: int, force: bool = False, cwd: str = None) -> dict:
    repo = main_repo_root(cwd)
    path = worktree_path(repo, issue)
    branch = _worktree_branch(repo, path)
    if not branch:
        raise WorkspaceError(f"no worktree for issue #{issue} at {path}")
    if not force:
        merged = json.loads(run(["gh", "pr", "list", "--head", branch, "--state", "merged", "--json", "number"],
                                cwd=repo).stdout or "[]")
        if not merged:
            raise WorkspaceError(f"no merged PR for {branch}; pass --force to discard the worktree anyway")
    run(["git", "worktree", "remove", "--force", path], cwd=repo)
    run(["git", "branch", "-D", branch], cwd=repo, check=False)
    run(["git", "worktree", "prune"], cwd=repo, check=False)
    return {"ok": True, "issue": issue, "removed_worktree": path, "deleted_branch": branch}


def main(argv: list = None) -> int:
    parser = argparse.ArgumentParser(description="Issue worktree helpers for the issue-orchestrator skill.")
    sub = parser.add_subparsers(dest="command", required=True)
    p_init = sub.add_parser("init", help="Create the issue worktree and dump the issue")
    p_init.add_argument("issue", type=int)
    p_init.add_argument("--slug", required=True)
    p_init.add_argument("--base", default="origin/main")
    p_rev = sub.add_parser("review-context", help="Write the diff and check results for the auditor")
    p_rev.add_argument("worktree")
    p_clean = sub.add_parser("cleanup", help="Remove a merged issue worktree and its local branch")
    p_clean.add_argument("issue", type=int)
    p_clean.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            out = cmd_init(args.issue, args.slug, args.base)
        elif args.command == "review-context":
            out = cmd_review_context(args.worktree)
        else:
            out = cmd_cleanup(args.issue, force=args.force)
    except (WorkspaceError, subprocess.TimeoutExpired, ValueError, OSError) as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 1
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
