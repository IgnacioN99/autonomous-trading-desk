#!/usr/bin/env python3
"""
install_guardian_service.py - Run the position guardian loop as a Windows Task Scheduler task (issue #55).

PROD resting entries (untriggered STOP_MARKET, LIMIT) need a live guardian loop with --interval <= 120s
(execute_futures_trade.check_guardian_alive). This installer registers the task \\TradingDesk\\PositionGuardian, which
starts the WSL distro and the loop at logon and every 5 min (a running loop makes the extra starts no-ops), and
restarts it after a failure:

  wsl.exe -d <distro> --cd <repo> -- python3 scripts/loops/position_guardian_loop.py --interval 60 --env <env>
      --log-file logs/guardian.log

Supported only inside WSL (Windows); Linux systemd and macOS launchd are out of scope for now (exit 2 elsewhere).

Usage:
  python3 scripts/install_guardian_service.py --install   [--env prod|testnet] [--distro <name>] [--dry-run]
  python3 scripts/install_guardian_service.py --uninstall [--dry-run]
  python3 scripts/install_guardian_service.py --status    [--env prod|testnet] [--json]

  --install    writes logs/guardian_task.xml (UTF-16 with BOM), then schtasks.exe /Create /XML ... /F and /Run.
  --uninstall  schtasks.exe /End (errors ignored), then /Delete /F.
  --status     schtasks.exe /Query /FO LIST /V (Status, Last Run Time, Last Result) plus check_guardian_alive(env);
               exit 0 when the guardian loop is alive for env, else 1.
  --dry-run    prints the task XML and the exact commands; writes and runs nothing.

The env defaults to utils.env_resolver.resolve_env() and the distro to $WSL_DISTRO_NAME. The task holds no secrets:
credentials keep loading from the repo .env. It never touches logs/guardian_state.json.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from xml.sax.saxutils import escape

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import execute_futures_trade as eft
from utils.env_resolver import resolve_env
from utils.dossier_provenance import _is_wsl

TASK_NAME = "\\TradingDesk\\PositionGuardian"
REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGS_DIR = os.path.join(REPO_DIR, "logs")
TASK_XML_NAME = "guardian_task.xml"
GUARDIAN_LOG_PATH = "logs/guardian.log"
GUARDIAN_INTERVAL_SECONDS = eft.GUARDIAN_MAX_INTERVAL_FOR_RESTING // 2
UNSUPPORTED_MESSAGE = "unsupported: only Windows (WSL) is supported for now"
# Second trigger: start the task every 5 min, indefinitely (no Duration), from a fixed past date. IgnoreNew skips it
# while the loop runs and the loop's single-instance lock makes any extra start exit 0, so it only restarts a loop
# that died, even if RestartOnFailure does not fire for a non-zero exit of the action.
RESTART_TRIGGER_INTERVAL = "PT5M"
RESTART_TRIGGER_START = "2020-01-01T00:00:00"

_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_.\-]+$")
_SAFE_PATH = re.compile(r"^[A-Za-z0-9_./\-]+$")


def is_wsl() -> bool:
    return _is_wsl()


def _run(cmd):
    """Runs cmd (a list, no shell) and returns (returncode, combined output). Every subprocess goes through here."""
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        return 127, f"{type(e).__name__}: {e}"
    return proc.returncode, (proc.stdout or b"").decode("utf-8", errors="replace")


def _cmdline_arg(value):
    """Quotes a value for the Windows command line that wsl.exe parses (spaces); a double quote is rejected."""
    if '"' in value:
        raise ValueError(f"unsupported character in {value!r}")
    return f'"{value}"' if any(c.isspace() for c in value) else value


def build_task_xml(distro, repo_path, env, interval, log_path, user_id=None) -> str:
    """Task Scheduler XML for the guardian task. Pure. user_id (DOMAIN\\user from whoami.exe) binds the logon
    trigger and the principal to that account; without it they apply to the account that registers the task."""
    if env not in ("prod", "testnet"):
        raise ValueError(f"env must be prod or testnet, got {env!r}")
    interval = int(interval)
    if not 10 <= interval <= eft.GUARDIAN_MAX_INTERVAL_FOR_RESTING:
        raise ValueError(f"interval must be between 10 and {eft.GUARDIAN_MAX_INTERVAL_FOR_RESTING}s, got {interval}")
    if not _SAFE_TOKEN.match(str(distro or "")):
        raise ValueError(f"invalid WSL distro name {distro!r}")
    if not str(repo_path or "").startswith("/"):
        raise ValueError(f"repo_path must be an absolute Linux path, got {repo_path!r}")
    if not _SAFE_PATH.match(str(log_path or "")):  # passed to the default shell after `--`
        raise ValueError(f"invalid log path {log_path!r}")
    arguments = (f"-d {distro} --cd {_cmdline_arg(repo_path)} -- python3 scripts/loops/position_guardian_loop.py "
                 f"--interval {interval} --env {env} --log-file {log_path}")
    user = f"\n      <UserId>{escape(user_id)}</UserId>" if user_id else ""
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Trading desk position guardian loop ({escape(env)}): protects resting entries and open positions.</Description>
    <URI>{escape(TASK_NAME)}</URI>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>{user}
    </LogonTrigger>
    <TimeTrigger>
      <Repetition>
        <Interval>{RESTART_TRIGGER_INTERVAL}</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
      <StartBoundary>{RESTART_TRIGGER_START}</StartBoundary>
      <Enabled>true</Enabled>
    </TimeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">{user}
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>999</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>wsl.exe</Command>
      <Arguments>{escape(arguments)}</Arguments>
    </Exec>
  </Actions>
</Task>
"""


def _windows_user():
    """DOMAIN\\user of the Windows account (whoami.exe), or None."""
    rc, out = _run(["whoami.exe"])
    user = (out or "").strip()
    if rc == 0 and re.match(r"^[^\s\\<>&]+\\[^\s\\<>&]+$", user):
        return user
    return None


def _create_cmd(winpath):
    return ["schtasks.exe", "/Create", "/TN", TASK_NAME, "/XML", winpath, "/F"]


def _resolve(env, distro):
    env = resolve_env(env)
    distro = distro or os.environ.get("WSL_DISTRO_NAME")
    return env, distro


def install(env=None, distro=None, dry_run=False, logs_dir=None) -> int:
    """Registers and starts the guardian task. Returns an exit code (0 ok, 1 failure, 2 unsupported)."""
    if not is_wsl():
        print(UNSUPPORTED_MESSAGE)
        return 2
    env, distro = _resolve(env, distro)
    if not distro:
        print("install: cannot determine the WSL distro ($WSL_DISTRO_NAME is not set); pass --distro <name>")
        return 1
    logs_dir = logs_dir or LOGS_DIR
    xml_path = os.path.join(logs_dir, TASK_XML_NAME)
    run_cmd = ["schtasks.exe", "/Run", "/TN", TASK_NAME]
    if dry_run:
        xml = build_task_xml(distro, REPO_DIR, env, GUARDIAN_INTERVAL_SECONDS, GUARDIAN_LOG_PATH)
        print(xml)
        print("# dry run: the trigger and principal get <UserId> from `whoami.exe` at install time")
        print(f"# would write {xml_path} (UTF-16 with BOM) and run:")
        for cmd in (["whoami.exe"], ["wslpath", "-w", xml_path], _create_cmd("<windows path of the XML>"), run_cmd):
            print(" ".join(cmd))
        return 0
    user_id = _windows_user()
    if user_id is None:
        print("install: whoami.exe did not return the Windows account; the task is registered for the current "
              "account without an explicit <UserId>")
    xml = build_task_xml(distro, REPO_DIR, env, GUARDIAN_INTERVAL_SECONDS, GUARDIAN_LOG_PATH, user_id=user_id)
    try:
        os.makedirs(logs_dir, exist_ok=True)
        with open(xml_path, "w", encoding="utf-16") as f:  # "utf-16" writes the BOM
            f.write(xml)
    except OSError as e:
        print(f"install: cannot write {xml_path}: {e}")
        return 1
    rc, out = _run(["wslpath", "-w", xml_path])
    winpath = (out or "").strip()
    if rc != 0 or not winpath:
        print(f"install: wslpath -w failed (rc {rc}): {winpath}")
        return 1
    rc, out = _run(_create_cmd(winpath))
    if rc != 0:
        print(f"install: schtasks /Create failed (rc {rc}): {(out or '').strip()}")
        return 1
    rc, out = _run(run_cmd)
    if rc != 0:
        print(f"install: task created but schtasks /Run failed (rc {rc}): {(out or '').strip()}; it starts at the "
              "next logon")
        return 1
    print(f"Installed and started {TASK_NAME}: guardian loop every {GUARDIAN_INTERVAL_SECONDS}s for {env} in WSL "
          f"distro {distro}, log {GUARDIAN_LOG_PATH}. Check it with: python3 scripts/install_guardian_service.py "
          f"--status --env {env}")
    return 0


def uninstall(dry_run=False) -> int:
    if not is_wsl():
        print(UNSUPPORTED_MESSAGE)
        return 2
    end_cmd = ["schtasks.exe", "/End", "/TN", TASK_NAME]
    delete_cmd = ["schtasks.exe", "/Delete", "/TN", TASK_NAME, "/F"]
    if dry_run:
        print("# dry run: would run:")
        for cmd in (end_cmd, delete_cmd):
            print(" ".join(cmd))
        return 0
    _run(end_cmd)  # not running is fine
    rc, out = _run(delete_cmd)
    if rc != 0:
        print(f"uninstall: schtasks /Delete failed (rc {rc}): {(out or '').strip()}")
        return 1
    print(f"Removed {TASK_NAME}.")
    return 0


# (field, English label, offset from the TaskName line in the fixed LIST field order: HostName, TaskName,
# Next Run Time, Status, Logon Mode, Last Run Time, Last Result, ...)
_QUERY_FIELDS = (("status", "Status", 2), ("last_run_time", "Last Run Time", 4), ("last_result", "Last Result", 5))


def parse_schtasks_query(out):
    """Status, Last Run Time and Last Result of the first record of `schtasks /Query /FO LIST /V`. English labels
    first; otherwise (localized Windows) the fixed field order after the line holding the task name. Missing
    values are None."""
    pairs = []
    for line in (out or "").splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip():
            pairs.append((key.strip(), value.strip()))
    labels = {}
    for k, v in pairs:
        labels.setdefault(k, v)
    english = any(label in labels for _, label, _ in _QUERY_FIELDS)
    name_idx = next((i for i, (_, v) in enumerate(pairs) if v.lower() == TASK_NAME.lower()), None)
    result = {}
    for field, label, offset in _QUERY_FIELDS:
        if english:
            result[field] = labels.get(label)
        elif name_idx is not None and name_idx + offset < len(pairs):
            result[field] = pairs[name_idx + offset][1]
        else:
            result[field] = None
    return result


def status(env=None, json_output=False, dry_run=False) -> int:
    if not is_wsl():
        print(UNSUPPORTED_MESSAGE)
        return 2
    env = resolve_env(env)
    query_cmd = ["schtasks.exe", "/Query", "/TN", TASK_NAME, "/FO", "LIST", "/V"]
    if dry_run:
        print("# dry run: would run:")
        print(" ".join(query_cmd))
        return 0
    rc, out = _run(query_cmd)
    task = parse_schtasks_query(out) if rc == 0 else {"status": None, "last_run_time": None, "last_result": None}
    alive, reason = eft.check_guardian_alive(env)
    report = {"task_name": TASK_NAME, "installed": rc == 0, **task, "env": env,
              "guardian_alive": bool(alive), "guardian_reason": reason}
    if json_output:
        print(json.dumps(report))
    else:
        if rc == 0:
            print(f"Task {TASK_NAME}: installed | status {task['status']} | last run {task['last_run_time']} | "
                  f"last result {task['last_result']}")
        else:
            print(f"Task {TASK_NAME}: not installed (schtasks /Query rc {rc})")
        print(f"Guardian loop ({env}): {'ALIVE' if alive else 'NOT ALIVE'} - {reason}")
        if not alive:
            print(f"Install or restart it: python3 scripts/install_guardian_service.py --install --env {env}")
    return 0 if alive else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description="Install the position guardian as a Windows Task Scheduler task (WSL)")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--install", action="store_true", help="Register and start the guardian task")
    action.add_argument("--uninstall", action="store_true", help="Stop and remove the guardian task")
    action.add_argument("--status", action="store_true", help="Task status plus guardian loop liveness")
    parser.add_argument("--dry-run", action="store_true", dest="dry_run", help="Print the XML and commands only")
    parser.add_argument("--env", choices=["prod", "testnet"], default=None,
                        help="Target environment; defaults to utils.env_resolver.resolve_env()")
    parser.add_argument("--distro", default=None, help="WSL distro name; defaults to $WSL_DISTRO_NAME")
    parser.add_argument("--json", action="store_true", dest="json_output", help="--status as one JSON object")
    args = parser.parse_args(argv)
    if not is_wsl():
        print(UNSUPPORTED_MESSAGE)
        return 2
    try:
        if args.install:
            return install(env=args.env, distro=args.distro, dry_run=args.dry_run)
        if args.uninstall:
            return uninstall(dry_run=args.dry_run)
        return status(env=args.env, json_output=args.json_output, dry_run=args.dry_run)
    except ValueError as e:
        print(f"error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
