#!/usr/bin/env python3
"""
scripts/hooks/post_pr_review_hook.py
PostToolUse Hook (agy "pr-review-trigger" / Claude Code .claude/settings.json): arms the in-session
multi-agent PR review.

This hook only keeps the pending-review marker (scripts/ci/pr_review_state.py,
logs/pr_review_state.json) up to date:
  * a successfully executed `gh pr create` or `git push` of a feature branch -> marker "pending"
    (the Stop hook pr_review_stop_hook.py then makes the agent run the /pr-review skill, which
    launches the reviewer subagents natively in the same session: agy invoke_subagent, Claude Code
    Agent tool). Only the program of each executed sub-command counts: the same words inside quoted
    strings, heredoc bodies or echo/grep arguments never arm. Pushes and PRs whose branch (refspec
    destination, `--head`, or the checked-out branch of the `cd`/`git -C` directory) is main/master
    never arm; a command that cannot be tokenized does not arm either (logged as "detect_error");
  * successful `gh pr comment ... --body-file .../pr_review/report.md` -> marker "done".
Failed commands change nothing. Events are logged to logs/pr_hook_events.jsonl.

Contract:
  Input (stdin): agy {toolCall:{name:"run_command", args:{CommandLine, Cwd?}}, conversationId, error?} or
                 Claude Code {tool_name:"Bash"|"PowerShell", tool_input:{command}, tool_response, session_id, cwd}
                 (Claude Code only fires PostToolUse on success; failed calls go to PostToolUseFailure).
  Output (stdout): {} (empty JSON object, valid for both runtimes), always with exit code 0.
"""

import os
import re
import sys
import json
import shlex
import subprocess
from typing import NamedTuple

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

try:
    from scripts.ci import pr_review_state as state_mod
except Exception as _import_error:  # pragma: no cover - the hook must still answer {} and exit 0
    state_mod = None
    sys.stderr.write(f"[PR REVIEW HOOK ERROR] cannot import pr_review_state: {_import_error}\n")

# Commands issued by the review flow itself never re-arm the review
REVIEW_FLOW_MARKERS = ("run_pr_audit", "triage_pr", "assemble_review", "verify_review", "pr_review_state")
PROTECTED_BRANCHES = ("main", "master")

# `<<WORD`, `<<-WORD`, `<<'WORD'`, `<< "WORD"`, `<<\WORD` (never the `<<<` here-string)
_HEREDOC_RE = re.compile(r"(?<!<)<<(?!<)([-~]?)[ \t]*\\?(['\"]?)([A-Za-z0-9_][A-Za-z0-9_.-]*)\2")
_ASSIGNMENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
_RESERVED_WORDS = {"!", "{", "do", "then", "else", "elif", "if", "while", "until"}
_SHELLS = {"bash", "sh", "zsh", "dash"}
_GIT_VALUE_OPTIONS = {"-c", "--git-dir", "--work-tree", "--namespace", "--config-env"}
_PUSH_VALUE_OPTIONS = {"--repo", "-o", "--push-option", "--receive-pack", "--exec"}
_MAX_UNWRAP_DEPTH = 4


class ReviewTrigger(NamedTuple):
    kind: str        # "push" | "pr_create"
    branch: str      # branch named in the command (refspec destination / --head), "" when not given
    directory: str   # directory the command ran in (payload cwd, then `cd` / `git -C`)


def _strip_heredoc_bodies(text: str) -> str:
    """Drops heredoc bodies and their terminator lines: their content is data, never a command."""
    out, pending = [], []
    for line in text.split("\n"):
        if pending:
            word, flag = pending[0]
            candidate = line.rstrip("\r")
            if flag == "-":
                candidate = candidate.lstrip("\t")
            elif flag == "~":
                candidate = candidate.lstrip()
            if candidate == word:
                pending.pop(0)
            continue
        out.append(line)
        pending = [(m.group(3), m.group(1)) for m in _HEREDOC_RE.finditer(line)]
    return "\n".join(out)


def _split_subcommands(text: str) -> list[list[str]]:
    """Words of every sub-command (split on && || ; | & ( ) and newlines). Raises ValueError (shlex)."""
    text = text.replace("\\\r\n", "").replace("\\\n", "").replace("\n", "\n;")
    lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    commands, current, skip_target = [], [], False
    for token in lexer:
        if token and all(c in lexer.punctuation_chars for c in token):
            if any(c in token for c in "<>") and not any(c in token for c in ";()"):
                if current and current[-1].isdigit():  # `2>&1`: the fd number is not an argument
                    current.pop()
                skip_target = True  # redirection: the next word is its target
                continue
            if current:
                commands.append(current)
            current, skip_target = [], False
            continue
        if skip_target:
            skip_target = False
            continue
        current.append(token)
    if current:
        commands.append(current)
    return commands


def _command_words(words: list[str]) -> list[str]:
    """Drops leading assignments, reserved words and the wrappers env/command/nohup/time/sudo/timeout/nice."""
    i, n = 0, len(words)
    while i < n:
        word = words[i]
        if _ASSIGNMENT_RE.match(word) or word in _RESERVED_WORDS:
            i += 1
        elif word == "env":
            i += 1
            while i < n and (words[i].startswith("-") or _ASSIGNMENT_RE.match(words[i])):
                i += 2 if words[i] in ("-u", "--unset", "-C", "--chdir", "-S", "--split-string") else 1
        elif word in ("command", "nohup", "time", "sudo"):
            i += 1
            while i < n and words[i].startswith("-"):
                i += 1
        elif word == "timeout":
            i += 1
            while i < n and words[i].startswith("-"):
                i += 2 if words[i] in ("-s", "--signal", "-k", "--kill-after") else 1
            i += 1  # duration
        elif word == "nice":
            i += 1
            while i < n and words[i].startswith("-"):
                i += 2 if words[i] == "-n" else 1
        else:
            break
    return words[i:]


def _program_name(word: str) -> str:
    name = re.split(r"[\\/]", word)[-1].lower()
    return name[:-4] if name.endswith(".exe") else name


def _change_dir(current: str, target: str, start: str) -> str:
    """`cd`/`git -C` target resolved against the tracked dir; `$VAR`, `~` or `-` -> unknown (start dir)."""
    if not target or target == "-" or "$" in target or target.startswith("~"):
        return start
    return os.path.normpath(os.path.join(current, target))


def _push_branch(args: list[str]) -> str:
    """Destination branch of the first refspec of `git push` args; "" without a refspec (or HEAD)."""
    positionals, skip, options_done = [], False, False
    for arg in args:
        if skip:
            skip = False
        elif options_done or not arg.startswith("-"):
            positionals.append(arg)
        elif arg == "--":
            options_done = True
        elif arg in _PUSH_VALUE_OPTIONS:
            skip = True
    if len(positionals) < 2:
        return ""
    spec = positionals[1]
    spec = spec[1:] if spec.startswith("+") else spec
    spec = spec.partition(":")[2] if ":" in spec else spec
    spec = spec[len("refs/heads/"):] if spec.startswith("refs/heads/") else spec
    return "" if spec in ("HEAD", "@") or _is_expansion(spec) else spec


def _is_expansion(word: str) -> bool:
    """`$VAR`, `$(...)` or backticks: the real value is unknown from the text."""
    return "$" in word or "`" in word


def _pr_head(args: list[str]) -> str:
    """`gh pr create --head/-H <branch>` (owner prefix stripped); "" when absent."""
    head = ""
    for i, arg in enumerate(args):
        if arg in ("--head", "-H") and i + 1 < len(args):
            head = args[i + 1]
        elif arg.startswith("--head="):
            head = arg[len("--head="):]
    return "" if _is_expansion(head) else head.split(":", 1)[-1]


def _scan_words(words: list[str], start: str, cwd: str, triggers: list, depth: int) -> str:
    """Records the push/PR-creation of one sub-command; returns the tracked directory after it."""
    words = _command_words(words)
    if not words:
        return cwd
    program, args = _program_name(words[0]), words[1:]
    if program == "cd":
        targets = [a for a in args if not (a.startswith("-") and a != "-")]
        return _change_dir(cwd, targets[0] if targets else "", start)
    if program in _SHELLS and depth < _MAX_UNWRAP_DEPTH:
        for i, arg in enumerate(args):
            if arg.startswith("--"):
                continue
            if not arg.startswith("-"):
                break  # `bash script.sh`: no inline script
            if "c" in arg[1:]:
                if i + 1 < len(args):
                    _scan_text(args[i + 1], start, cwd, triggers, depth + 1)
                break
        return cwd
    if program == "wsl" and depth < _MAX_UNWRAP_DEPTH:
        i = 0
        while i < len(args) and args[i].startswith("-"):
            if args[i] in ("--", "-e", "--exec"):
                i += 1
                break
            i += 2 if args[i] in ("-d", "--distribution", "-u", "--user", "--cd") else 1
        _scan_words(args[i:], start, cwd, triggers, depth + 1)
        return cwd
    if program == "git":
        directory, i = cwd, 0
        while i < len(args) and args[i].startswith("-"):
            if args[i] == "-C" and i + 1 < len(args):
                directory = _change_dir(directory, args[i + 1], start)
                i += 2
            else:
                i += 2 if args[i] in _GIT_VALUE_OPTIONS else 1
        if i < len(args) and args[i] == "push":
            triggers.append(ReviewTrigger("push", _push_branch(args[i + 1:]), directory))
        return cwd
    if program == "gh" and args[:2] == ["pr", "create"]:
        triggers.append(ReviewTrigger("pr_create", _pr_head(args[2:]), cwd))
    return cwd


def _scan_text(text: str, start: str, cwd: str, triggers: list, depth: int = 0) -> str:
    for words in _split_subcommands(_strip_heredoc_bodies(text)):
        cwd = _scan_words(words, start, cwd, triggers, depth)
    return cwd


def find_review_trigger(command: str, cwd: str | None = None) -> ReviewTrigger | None:
    """Last executed `git push` / `gh pr create` sub-command of `command` (no git calls), or None.

    Raises ValueError when the command cannot be tokenized (the caller must not arm)."""
    if not command or ("push" not in command and "create" not in command):
        return None
    # Avoid recursive execution if the command itself was running the review flow
    if any(marker in command for marker in REVIEW_FLOW_MARKERS):
        return None
    triggers: list[ReviewTrigger] = []
    start = cwd or REPO_ROOT
    _scan_text(command, start, start, triggers)
    return triggers[-1] if triggers else None


def is_pr_creation_or_push(command: str) -> bool:
    """True when the command executes `gh pr create` or `git push` and the branch it names is not main/master."""
    try:
        trigger = find_review_trigger(command)
    except ValueError:
        return False
    return trigger is not None and trigger.branch not in PROTECTED_BRANCHES


def is_review_post(command: str) -> bool:
    """`gh pr comment <n> --body-file <...>/pr_review/report.md` (the /pr-review publication step)."""
    if not command or not re.search(r"\bgh\s+pr\s+comment\b", command):
        return False
    return bool(re.search(r"--body-file[=\s]+['\"]?\S*pr_review/report\.md", command))


def _git(args: list[str], directory: str | None = None) -> str:
    try:
        res = subprocess.run(["git", "-C", directory or REPO_ROOT] + args, cwd=REPO_ROOT,
                             capture_output=True, text=True, timeout=5)
        return res.stdout.strip() if res.returncode == 0 else ""
    except Exception:
        return ""


def resolve_branch_and_sha(trigger: ReviewTrigger) -> tuple[str, str]:
    """(branch, head_sha) of a trigger: the branch named in the command, else the checked-out branch of
    its directory ("" when unresolved or detached); head_sha of that branch, else of HEAD, else ""."""
    branch, head_sha = trigger.branch, ""
    if branch:
        if not branch.startswith("-"):
            head_sha = _git(["rev-parse", "--verify", "--quiet", branch], trigger.directory)
    else:
        branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], trigger.directory)
        branch = "" if branch == "HEAD" else branch
    if not head_sha:
        head_sha = _git(["rev-parse", "HEAD"], trigger.directory)
    return branch, head_sha


def is_claude_payload(payload: dict) -> bool:
    return "toolCall" not in payload and ("tool_name" in payload or "hook_event_name" in payload)


def extract_command(payload: dict) -> str:
    tool_call = payload.get("toolCall") if isinstance(payload.get("toolCall"), dict) else {}
    args = tool_call.get("args") if isinstance(tool_call.get("args"), dict) else {}
    command_line = args.get("CommandLine", "")
    if not command_line and isinstance(payload.get("tool_input"), dict):
        # Claude Code PostToolUse payload: only the Bash and PowerShell tools run shell commands
        if payload.get("tool_name", "Bash") not in ("Bash", "PowerShell"):
            return ""
        command_line = payload["tool_input"].get("command", "")
    return command_line if isinstance(command_line, str) else ""


def extract_cwd(payload: dict) -> str:
    """Directory the command started in: agy toolCall.args.Cwd or Claude Code cwd, else REPO_ROOT."""
    tool_call = payload.get("toolCall") if isinstance(payload.get("toolCall"), dict) else {}
    args = tool_call.get("args") if isinstance(tool_call.get("args"), dict) else {}
    cwd = args.get("Cwd") or payload.get("cwd")
    return cwd if isinstance(cwd, str) and os.path.isdir(cwd) else REPO_ROOT


def conversation_of(payload: dict) -> str:
    """agy conversationId or Claude Code session_id (the Stop hook compares against the same field)."""
    return str(payload.get("conversationId") or payload.get("session_id") or "")


def tool_failed(payload: dict) -> bool:
    if payload.get("error"):
        return True
    if payload.get("hook_event_name") == "PostToolUseFailure":
        return True
    response = payload.get("tool_response")
    if isinstance(response, dict):
        if response.get("is_error") or response.get("interrupted"):
            return True
        for key in ("exit_code", "exitCode", "returncode"):
            code = response.get(key)
            if isinstance(code, int) and code != 0:
                return True
    return False


def handle_post_tool_use(payload: dict, path: str | None = None) -> str:
    """Updates the marker for one PostToolUse payload. Returns the action taken (for tests/logs)."""
    command_line = extract_command(payload)
    if not command_line or tool_failed(payload):
        return "ignored"

    if is_review_post(command_line):
        if state_mod.mark_done("posted", path=path):
            state_mod.log_event({"event": "review_posted", "trigger_command": command_line[:300]}, path)
            sys.stderr.write("[PR REVIEW HOOK] Review comment posted: pending auto-review closed.\n")
            return "done"
        return "ignored"

    try:
        trigger = find_review_trigger(command_line, extract_cwd(payload))
    except ValueError as exc:
        # Arming is a convenience: an unparsable command never arms (a false arm blocks the stop up to 3 times)
        state_mod.log_event({"event": "detect_error", "error": str(exc), "trigger_command": command_line[:300]},
                            path)
        sys.stderr.write(f"[PR REVIEW HOOK] Cannot tokenize the command ({exc}): auto-review not armed.\n")
        return "ignored"

    if trigger is not None:
        branch, head_sha = resolve_branch_and_sha(trigger)
        if branch in PROTECTED_BRANCHES:
            return "ignored"
        state = state_mod.mark_pending(
            trigger_command=command_line,
            branch=branch,
            head_sha=head_sha,
            conversation_id=conversation_of(payload),
            path=path,
        )
        state_mod.log_event({"event": "review_pending", "trigger_command": command_line[:300],
                             "branch": branch, "conversation_id": state["conversation_id"]}, path)
        sys.stderr.write(
            f"[PR REVIEW HOOK] PR creation/push detected on branch '{branch}': multi-agent review pending.\n"
            "   It runs in this session via the /pr-review skill (reviewer subagents) when the turn ends.\n"
        )
        return "pending"

    return "ignored"


def main():
    try:
        raw_input = sys.stdin.read()
        payload = json.loads(raw_input) if raw_input.strip() else {}
        if isinstance(payload, dict) and state_mod is not None:
            handle_post_tool_use(payload)
    except Exception as e:
        sys.stderr.write(f"[PR REVIEW HOOK ERROR] {e}\n")

    # Contract requirement: PostToolUse must return {} on stdout
    print(json.dumps({}))


if __name__ == "__main__":
    main()
