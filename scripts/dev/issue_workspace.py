#!/usr/bin/env python3
"""
scripts/dev/issue_workspace.py - Deterministic helpers for the issue-orchestrator skill (development work only;
it never touches the exchange, the ledger or any desk runtime file).

  init <N> --slug <slug> [--base origin/main]
      git fetch origin; create the sibling worktree <repo>-wt-issue-<N> on a new branch fix/issue-<N>-<slug> from
      <base>; dump the open issue (gh issue view) to <worktree>/logs/issue_work/issue.json, with a
      deterministic "routing" block ({"route": quick|build|deep, "risk": low|medium|high, "reason": ...})
      computed from its labels and title by classify_issue().
  record-route <N> --from <json file>
      Validate the orchestrator's route record (final route, fixer/auditor models and efforts per round,
      escalations, approved round, merged) and append one JSON line to logs/issue_routing.jsonl in the MAIN
      checkout, so it survives cleanup. route_auto is read from the worktree's issue.json (null if absent).
      Run it before cleanup.
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
`checks_ok`), 1 failure, 2 invalid arguments (including an invalid or unreadable record-route file).
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
ROUTING_LOG = os.path.join("logs", "issue_routing.jsonl")

ROUTES = ("quick", "build", "deep")  # ascending rank: a final route may upgrade, never downgrade
DEEP_LABELS = {"cat:risk_gate", "severity:high", "severity:critical"}
QUICK_LOW_CATEGORIES = {"cat:infra", "cat:tool_error"}
# Word boundaries treat "_" and "-" as separators ("pre_trade_guard" matches "guard") but keep "aggregate" and
# "safeguard" from matching "gate" / "guard".
DEEP_TITLE_RE = re.compile(
    r"(?<![a-z0-9])(execute_futures_trade|isolated_market_evaluator|executor|guardian|guards?|gates?|hooks?|"
    r"stop[- ]loss\s+verification|stop\s+verification|evaluator\s+prompt)(?![a-z0-9])",
    re.IGNORECASE)
ROUTE_MODELS = {"haiku", "sonnet", "opus", "fable"}
ROUTE_EFFORTS = {"low", "medium", "high", "xhigh", "max", "default"}
ESCALATION_KINDS = {"effort", "capability"}
MAX_FIXER_ROUNDS = 3
ROUTE_RECORD_REQUIRED = {"route_final", "fixer_models", "auditor_model", "approved_round", "escalations", "merged"}
ROUTE_RECORD_KEYS = ROUTE_RECORD_REQUIRED | {"upgrade_reason"}


def _checks(python: str) -> list:
    return [
        ("compileall", [python, "-m", "compileall", "-q", "scripts/", "tests/"]),
        ("sync_claude_assets", [python, "scripts/dev/sync_claude_assets.py", "--check"]),
        ("unittest", [python, "-m", "unittest", "discover", "tests/"]),
    ]


class WorkspaceError(Exception):
    pass


class RouteRecordError(WorkspaceError):
    """Invalid record-route input (exit 2)."""


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
# triage
# --------------------------------------------------------------------------------------------------
def _label_names(labels) -> list:
    """Lower-cased label names from gh dicts ({"name": ...}) and/or plain strings; anything else is ignored."""
    if not isinstance(labels, list):
        return []
    names = []
    for item in labels:
        name = item.get("name") if isinstance(item, dict) else item
        if isinstance(name, str) and name.strip():
            names.append(name.strip().lower())
    return names


def classify_issue(title, labels) -> dict:
    """Deterministic route for the issue workflow (first match wins): deep/high, quick/low, else build/medium."""
    title = title if isinstance(title, str) else ""
    names = _label_names(labels)
    deep_label = next((n for n in names if n in DEEP_LABELS), None)
    if deep_label:
        return {"route": "deep", "risk": "high", "reason": f"label {deep_label}"}
    match = DEEP_TITLE_RE.search(title)
    if match:
        return {"route": "deep", "risk": "high", "reason": f"title keyword '{match.group(0).lower()}'"}
    if "documentation" in names:
        return {"route": "quick", "risk": "low", "reason": "label documentation"}
    if "severity:low" in names:
        category = next((n for n in names if n in QUICK_LOW_CATEGORIES), None)
        if category:
            return {"route": "quick", "risk": "low", "reason": f"severity:low with {category}"}
    return {"route": "build", "risk": "medium", "reason": "default (no deep/quick rule matched)"}


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
    data["routing"] = classify_issue(data.get("title"), data.get("labels"))
    with open(os.path.join(work, "issue.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    head = run(["git", "rev-parse", "--short", "HEAD"], cwd=path).stdout.strip()
    return {"ok": True, "issue": issue, "title": data.get("title"), "worktree": path, "branch": branch,
            "base": base, "head": head, "work_dir": work, "issue_file": os.path.join(work, "issue.json"),
            "routing": data["routing"]}


# --------------------------------------------------------------------------------------------------
# record-route
# --------------------------------------------------------------------------------------------------
def _route_auto(worktree: str):
    """routing.route from the worktree's issue.json, or None when the file or key is missing or unreadable."""
    try:
        with open(os.path.join(worktree, WORK_DIR, "issue.json"), encoding="utf-8") as f:
            routing = json.load(f).get("routing")
    except (OSError, ValueError, AttributeError):
        return None
    route = routing.get("route") if isinstance(routing, dict) else None
    return route if route in ROUTES else None


def _model_entry(value, where: str) -> dict:
    if not isinstance(value, dict) or set(value) != {"model", "effort"}:
        raise RouteRecordError(f"{where} must be an object with exactly 'model' and 'effort'")
    if not isinstance(value["model"], str) or value["model"] not in ROUTE_MODELS:
        raise RouteRecordError(f"{where}.model {value['model']!r} not in {sorted(ROUTE_MODELS)}")
    if not isinstance(value["effort"], str) or value["effort"] not in ROUTE_EFFORTS:
        raise RouteRecordError(f"{where}.effort {value['effort']!r} not in {sorted(ROUTE_EFFORTS)}")
    return {"model": value["model"], "effort": value["effort"]}


def _validate_route_record(rec, route_auto) -> dict:
    if not isinstance(rec, dict):
        raise RouteRecordError("route record must be a JSON object")
    unknown = sorted(set(rec) - ROUTE_RECORD_KEYS)
    if unknown:
        raise RouteRecordError(f"unknown keys: {', '.join(unknown)}")
    missing = sorted(ROUTE_RECORD_REQUIRED - set(rec))
    if missing:
        raise RouteRecordError(f"missing keys: {', '.join(missing)}")
    route_final = rec["route_final"]
    if not isinstance(route_final, str) or route_final not in ROUTES:
        raise RouteRecordError(f"route_final {route_final!r} not in {list(ROUTES)}")
    if route_auto is not None and ROUTES.index(route_final) < ROUTES.index(route_auto):
        raise RouteRecordError(f"route_final {route_final} downgrades route_auto {route_auto} (upgrade only)")
    reason = rec.get("upgrade_reason")
    if reason is not None and not isinstance(reason, str):
        raise RouteRecordError("upgrade_reason must be a string or null")
    if route_auto is not None and route_final != route_auto and not (reason or "").strip():
        raise RouteRecordError(f"upgrade_reason is required: route_final {route_final} differs from route_auto "
                               f"{route_auto}")
    fixers = rec["fixer_models"]
    if not isinstance(fixers, list) or not 1 <= len(fixers) <= MAX_FIXER_ROUNDS:
        raise RouteRecordError(f"fixer_models must be a list of 1-{MAX_FIXER_ROUNDS} entries (one per fixer round)")
    fixer_models = [_model_entry(v, f"fixer_models[{i}]") for i, v in enumerate(fixers)]
    auditor_model = _model_entry(rec["auditor_model"], "auditor_model")
    rounds = len(fixer_models)
    approved = rec["approved_round"]
    if isinstance(approved, bool) or not isinstance(approved, int) or not 0 <= approved <= rounds:
        raise RouteRecordError(f"approved_round must be an integer 0-{rounds} (0 = never approved)")
    escalations = rec["escalations"]
    if not isinstance(escalations, list) or len(escalations) != rounds - 1:
        raise RouteRecordError(f"escalations must list exactly {rounds - 1} entries (one per round after the first)")
    for i, esc in enumerate(escalations):
        if not isinstance(esc, dict) or set(esc) != {"round", "kind"}:
            raise RouteRecordError(f"escalations[{i}] must be an object with exactly 'round' and 'kind'")
        if isinstance(esc["round"], bool) or not isinstance(esc["round"], int) or esc["round"] != i + 2:
            raise RouteRecordError(f"escalations[{i}].round must be {i + 2}")
        if not isinstance(esc["kind"], str) or esc["kind"] not in ESCALATION_KINDS:
            raise RouteRecordError(f"escalations[{i}].kind {esc['kind']!r} not in {sorted(ESCALATION_KINDS)}")
    if not isinstance(rec["merged"], bool):
        raise RouteRecordError("merged must be true or false")
    return {"route_final": route_final, "upgrade_reason": reason, "fixer_models": fixer_models,
            "auditor_model": auditor_model, "rounds": rounds, "approved_round": approved,
            "escalations": [{"round": e["round"], "kind": e["kind"]} for e in escalations], "merged": rec["merged"]}


def cmd_record_route(issue: int, src: str, cwd: str = None) -> dict:
    try:
        with open(src, encoding="utf-8") as f:
            rec = json.load(f)
    except (OSError, ValueError) as e:
        raise RouteRecordError(f"cannot read route record {src}: {e}")
    repo = main_repo_root(cwd)
    route_auto = _route_auto(worktree_path(repo, issue))
    fields = _validate_route_record(rec, route_auto)
    record = {"schema": 1, "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "issue": issue,
              "route_auto": route_auto, **fields}
    log = os.path.join(repo, ROUTING_LOG)
    os.makedirs(os.path.dirname(log), exist_ok=True)
    with open(log, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, sort_keys=True) + "\n")
    return {"ok": True, "issue": issue, "log": log, "record": record}


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
    p_route = sub.add_parser("record-route", help="Append the issue's route record to logs/issue_routing.jsonl "
                                                  "in the main checkout (run before cleanup)")
    p_route.add_argument("issue", type=int)
    p_route.add_argument("--from", dest="src", required=True,
                         help="JSON file with route_final, upgrade_reason, fixer_models, auditor_model, "
                              "approved_round, escalations, merged")
    p_rev =sub.add_parser("review-context", help="Write the diff and check results for the auditor")
    p_rev.add_argument("worktree")
    p_clean = sub.add_parser("cleanup", help="Remove a merged issue worktree and its local branch")
    p_clean.add_argument("issue", type=int)
    p_clean.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            out = cmd_init(args.issue, args.slug, args.base)
        elif args.command == "record-route":
            out = cmd_record_route(args.issue, args.src)
        elif args.command == "review-context":
            out = cmd_review_context(args.worktree)
        else:
            out = cmd_cleanup(args.issue, force=args.force)
    except RouteRecordError as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 2
    except (WorkspaceError, subprocess.TimeoutExpired, ValueError, OSError) as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 1
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
