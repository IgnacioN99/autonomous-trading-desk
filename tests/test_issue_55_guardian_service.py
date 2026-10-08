#!/usr/bin/env python3
"""
test_issue_55_guardian_service.py - Issue #55: the position guardian as a Windows Task Scheduler service.

1. Installer (scripts/install_guardian_service.py): task XML (interval <= GUARDIAN_MAX_INTERVAL_FOR_RESTING, env,
   repo path, distro, restart settings, InteractiveToken, IgnoreNew, no secrets, valid XML); --install / --uninstall /
   --status call the right schtasks.exe argv; the XML is written UTF-16 with BOM; --dry-run writes and runs nothing;
   outside WSL it exits 2; logs/guardian_state.json is never touched.
2. Guardian loop: default interval 60, a warning above 120, --log-file tees and rotates, a single-instance lock makes a
   second loop exit 0 and --once ignores the lock.
3. Doctor: WARN with the install hint when the loop is not alive (exit code unchanged), OK when it is.
4. Onboarding: in WSL a "y" calls the installer, "n" does not; outside WSL the question is not asked.

Hermetic: every subprocess goes through a mocked _run, every exchange call is faked and every file is in a temp dir.
"""

import io
import os
import errno
import sys
import json
import time
import codecs
import tempfile
import unittest
import contextlib
import xml.etree.ElementTree as ET
from unittest.mock import patch, MagicMock

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(BASE_DIR, "scripts")
LOOPS_DIR = os.path.join(SCRIPTS_DIR, "loops")
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
for p in (SCRIPTS_DIR, LOOPS_DIR, TESTS_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None

import execute_futures_trade as eft
import position_guardian_loop as pgl
import install_guardian_service as isg
import trading_doctor
import sync_session_state as sss
import user_profile as up
from test_exit_management import FakeExchange, offline
from test_pending_entries import write_guardian_state

NS = "{http://schemas.microsoft.com/windows/2004/02/mit/task}"
REPO = "/mnt/c/Users/me/trading"


def parse(xml_text):
    return ET.fromstring(xml_text.encode("utf-16"))


def capture(fn, *args, **kwargs):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = fn(*args, **kwargs)
    return code, out.getvalue(), err.getvalue()


class FakeRun:
    """Records every argv given to install_guardian_service._run and answers like the Windows tools."""

    def __init__(self, responses=None):
        self.calls = []
        self.responses = dict(responses or {})

    def __call__(self, cmd):
        self.calls.append(list(cmd))
        key = cmd[0] if cmd[0] != "schtasks.exe" else f"schtasks.exe {cmd[1]}"
        defaults = {"whoami.exe": (0, "desktop-pc\\me\r\n"), "wslpath": (0, "C:\\repo\\logs\\guardian_task.xml\n")}
        return self.responses.get(key, defaults.get(key, (0, "SUCCESS")))


class TestTaskXml(unittest.TestCase):
    def build(self, **kw):
        args = dict(distro="Ubuntu-22.04", repo_path=REPO, env="prod", interval=isg.GUARDIAN_INTERVAL_SECONDS,
                    log_path=isg.GUARDIAN_LOG_PATH)
        args.update(kw)
        return isg.build_task_xml(**args)

    def test_interval_is_half_the_resting_limit(self):
        self.assertEqual(isg.GUARDIAN_INTERVAL_SECONDS, eft.GUARDIAN_MAX_INTERVAL_FOR_RESTING // 2)
        self.assertLessEqual(isg.GUARDIAN_INTERVAL_SECONDS, eft.GUARDIAN_MAX_INTERVAL_FOR_RESTING)
        with self.assertRaises(ValueError):
            self.build(interval=eft.GUARDIAN_MAX_INTERVAL_FOR_RESTING + 1)

    def test_valid_xml_with_action_env_repo_and_distro(self):
        root = parse(self.build(env="testnet"))
        self.assertEqual(root.find(f"{NS}RegistrationInfo/{NS}URI").text, "\\TradingDesk\\PositionGuardian")
        execs = root.findall(f"{NS}Actions/{NS}Exec")
        self.assertEqual(len(execs), 1)
        self.assertEqual(execs[0].find(f"{NS}Command").text, "wsl.exe")
        self.assertEqual(execs[0].find(f"{NS}Arguments").text,
                         f"-d Ubuntu-22.04 --cd {REPO} -- python3 scripts/loops/position_guardian_loop.py "
                         f"--interval 60 --env testnet --log-file logs/guardian.log")
        self.assertIsNotNone(root.find(f"{NS}Triggers/{NS}LogonTrigger"))

    def test_repeating_time_trigger_revives_a_dead_loop(self):
        triggers = list(parse(self.build()).find(f"{NS}Triggers"))
        self.assertEqual([t.tag for t in triggers], [f"{NS}LogonTrigger", f"{NS}TimeTrigger"])
        tt = triggers[1]
        self.assertEqual(tt.find(f"{NS}Repetition/{NS}Interval").text, "PT5M")
        self.assertIsNone(tt.find(f"{NS}Repetition/{NS}Duration"), "no Duration: repeats indefinitely")
        self.assertEqual(tt.find(f"{NS}Repetition/{NS}StopAtDurationEnd").text, "false")
        self.assertEqual(tt.find(f"{NS}Enabled").text, "true")
        self.assertLess(tt.find(f"{NS}StartBoundary").text, time.strftime("%Y-%m-%dT%H:%M:%S"), "a past start")

    def test_settings_principal_and_restart(self):
        root = parse(self.build(user_id="desktop-pc\\me"))
        s = root.find(f"{NS}Settings")
        self.assertEqual(s.find(f"{NS}MultipleInstancesPolicy").text, "IgnoreNew")
        self.assertEqual(s.find(f"{NS}DisallowStartIfOnBatteries").text, "false")
        self.assertEqual(s.find(f"{NS}StopIfGoingOnBatteries").text, "false")
        self.assertEqual(s.find(f"{NS}ExecutionTimeLimit").text, "PT0S")
        self.assertEqual(s.find(f"{NS}StartWhenAvailable").text, "true")
        self.assertEqual(s.find(f"{NS}RestartOnFailure/{NS}Interval").text, "PT1M")
        self.assertEqual(s.find(f"{NS}RestartOnFailure/{NS}Count").text, "999")
        pr = root.find(f"{NS}Principals/{NS}Principal")
        self.assertEqual(pr.find(f"{NS}LogonType").text, "InteractiveToken")
        self.assertEqual(pr.find(f"{NS}RunLevel").text, "LeastPrivilege")
        self.assertEqual(pr.find(f"{NS}UserId").text, "desktop-pc\\me")
        self.assertEqual(root.find(f"{NS}Triggers/{NS}LogonTrigger/{NS}UserId").text, "desktop-pc\\me")
        self.assertIsNone(root.find(f"{NS}Principals/{NS}Principal/{NS}Password"))

    def test_no_secrets_from_the_environment(self):
        secrets = {"BINANCE_API_KEY": "kKkKkK123456", "BINANCE_API_SECRET": "sSsSsS654321",
                   "NOTION_TOKEN": "tTtTtT999888", "GITHUB_TOKEN": "ghp_zzzzzz777"}
        with patch.dict(os.environ, secrets):
            xml_text = self.build()
            for name, value in os.environ.items():
                if any(k in name.upper() for k in ("KEY", "SECRET", "TOKEN")) and len(value) >= 6:
                    self.assertNotIn(value, xml_text, name)
        for marker in ("KEY", "SECRET", "TOKEN", "Password"):
            self.assertNotIn(marker, xml_text)

    def test_repo_path_with_spaces_is_quoted_and_unsafe_values_rejected(self):
        args = parse(self.build(repo_path="/mnt/c/Users/My Name/trading")).find(f"{NS}Actions/{NS}Exec/{NS}Arguments")
        self.assertIn('--cd "/mnt/c/Users/My Name/trading" --', args.text)
        for bad in (dict(distro="Ubuntu; rm"), dict(env="mainnet"), dict(repo_path="relative/path"),
                    dict(log_path="logs/g.log; curl x"), dict(repo_path='/mnt/c/"x"')):
            with self.assertRaises(ValueError, msg=bad):
                self.build(**bad)


class TestInstallerCommands(unittest.TestCase):
    def setUp(self):
        self.logs = tempfile.mkdtemp()
        self.ws = tempfile.mkdtemp()
        self.fake = FakeRun()
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(isg, "is_wsl", return_value=True))
        stack.enter_context(patch.object(isg, "_run", side_effect=self.fake))
        stack.enter_context(patch.object(isg, "LOGS_DIR", self.logs))
        stack.enter_context(patch("execute_futures_trade._workspace_dir", return_value=self.ws))
        stack.enter_context(patch.dict(os.environ, {"WSL_DISTRO_NAME": "Ubuntu"}))

    def test_install_writes_utf16_xml_and_creates_then_runs_the_task(self):
        code, out, _ = capture(isg.main, ["--install", "--env", "prod"])
        self.assertEqual(code, 0, out)
        xml_path = os.path.join(self.logs, "guardian_task.xml")
        self.assertEqual(self.fake.calls, [
            ["whoami.exe"],
            ["wslpath", "-w", xml_path],
            ["schtasks.exe", "/Create", "/TN", "\\TradingDesk\\PositionGuardian", "/XML",
             "C:\\repo\\logs\\guardian_task.xml", "/F"],
            ["schtasks.exe", "/Run", "/TN", "\\TradingDesk\\PositionGuardian"],
        ])
        with open(xml_path, "rb") as f:
            raw = f.read()
        self.assertTrue(raw.startswith(codecs.BOM_UTF16_LE) or raw.startswith(codecs.BOM_UTF16_BE))
        root = ET.fromstring(raw)
        args = root.find(f"{NS}Actions/{NS}Exec/{NS}Arguments").text
        self.assertIn("-d Ubuntu --cd " + isg.REPO_DIR + " -- ", args)
        self.assertIn("--interval 60 --env prod --log-file logs/guardian.log", args)
        self.assertEqual(root.find(f"{NS}Triggers/{NS}LogonTrigger/{NS}UserId").text, "desktop-pc\\me")
        self.assertEqual(os.listdir(self.logs), ["guardian_task.xml"], "never touches guardian_state.json")
        self.assertEqual(os.listdir(self.ws), [])

    def test_install_stops_when_create_fails(self):
        self.fake.responses["schtasks.exe /Create"] = (1, "ERROR: Access is denied.")
        code, out, _ = capture(isg.install, env="prod")
        self.assertEqual(code, 1)
        self.assertIn("Access is denied", out)
        self.assertNotIn(["schtasks.exe", "/Run", "/TN", isg.TASK_NAME], self.fake.calls)

    def test_dry_run_writes_and_runs_nothing(self):
        code, out, _ = capture(isg.main, ["--install", "--env", "prod", "--dry-run", "--distro", "Debian"])
        self.assertEqual(code, 0)
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(os.listdir(self.logs), [])
        self.assertIn("<Command>wsl.exe</Command>", out)
        self.assertIn("-d Debian --cd", out)
        self.assertIn("schtasks.exe /Create /TN \\TradingDesk\\PositionGuardian /XML", out)
        self.assertIn("schtasks.exe /Run /TN \\TradingDesk\\PositionGuardian", out)
        code, out, _ = capture(isg.main, ["--uninstall", "--dry-run"])
        self.assertEqual((code, self.fake.calls), (0, []))
        self.assertIn("schtasks.exe /Delete /TN \\TradingDesk\\PositionGuardian /F", out)

    def test_uninstall_ends_then_deletes_ignoring_end_errors(self):
        self.fake.responses["schtasks.exe /End"] = (1, "ERROR: The task is not running.")
        code, _, _ = capture(isg.main, ["--uninstall"])
        self.assertEqual(code, 0)
        self.assertEqual(self.fake.calls, [
            ["schtasks.exe", "/End", "/TN", "\\TradingDesk\\PositionGuardian"],
            ["schtasks.exe", "/Delete", "/TN", "\\TradingDesk\\PositionGuardian", "/F"],
        ])
        self.fake.responses["schtasks.exe /Delete"] = (1, "ERROR: The system cannot find the file specified.")
        self.assertEqual(capture(isg.main, ["--uninstall"])[0], 1)

    QUERY_EN = ("\r\nFolder: \\TradingDesk\r\nHostName:      DESKTOP-PC\r\nTaskName:      \\TradingDesk\\PositionGuardian\r\n"
                "Next Run Time: N/A\r\nStatus:        Running\r\nLogon Mode:    Interactive only\r\n"
                "Last Run Time: 10/8/2026 9:00:00 AM\r\nLast Result:   267009\r\nAuthor:        desktop-pc\\me\r\n")

    def test_status_json_with_task_and_live_guardian(self):
        write_guardian_state(self.ws, env="prod", age=5)
        self.fake.responses["schtasks.exe /Query"] = (0, self.QUERY_EN)
        code, out, _ = capture(isg.main, ["--status", "--env", "prod", "--json"])
        self.assertEqual(code, 0, out)
        self.assertEqual(self.fake.calls, [["schtasks.exe", "/Query", "/TN", "\\TradingDesk\\PositionGuardian",
                                            "/FO", "LIST", "/V"]])
        report = json.loads(out)
        self.assertEqual((report["installed"], report["status"], report["last_run_time"], report["last_result"]),
                         (True, "Running", "10/8/2026 9:00:00 AM", "267009"))
        self.assertTrue(report["guardian_alive"], report["guardian_reason"])
        self.assertEqual(report["env"], "prod")

    def test_status_not_installed_and_guardian_down(self):
        self.fake.responses["schtasks.exe /Query"] = (1, "ERROR: The system cannot find the file specified.")
        code, out, _ = capture(isg.main, ["--status", "--env", "prod"])
        self.assertEqual(code, 1)
        self.assertIn("not installed", out)
        self.assertIn("NOT ALIVE", out)
        self.assertIn("python3 scripts/install_guardian_service.py --install --env prod", out)

    def test_status_parses_localized_list_by_position(self):
        es = ("Carpeta: \\TradingDesk\r\nNombre de host:   DESKTOP-PC\r\nNombre de tarea:  \\TradingDesk\\PositionGuardian\r\n"
              "Hora próxima ejecución: N/D\r\nEstado:   En ejecución\r\nModo de inicio de sesión: Solo interactivo\r\n"
              "Hora última ejecución: 8/10/2026 9:00:00\r\nÚltimo resultado: 0\r\n")
        self.assertEqual(isg.parse_schtasks_query(es), {"status": "En ejecución", "last_run_time": "8/10/2026 9:00:00",
                                                        "last_result": "0"})
        self.assertEqual(isg.parse_schtasks_query(""), {"status": None, "last_run_time": None, "last_result": None})

    def test_outside_wsl_exits_2(self):
        with patch.object(isg, "is_wsl", return_value=False):
            for argv in (["--install"], ["--install", "--dry-run"], ["--uninstall"], ["--status"]):
                code, out, _ = capture(isg.main, argv)
                self.assertEqual(code, 2, argv)
                self.assertIn("unsupported: only Windows (WSL) is supported for now", out)
            self.assertEqual(capture(isg.install)[0], 2)
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(os.listdir(self.logs), [])


class TestGuardianLoopService(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp()
        self.log_dir = os.path.join(self.ws, "logs")

    def run_loop(self, argv, cycle=None):
        stack = contextlib.ExitStack()
        with stack:
            stack.enter_context(offline(FakeExchange([]), workspace=self.ws))
            stack.enter_context(patch.object(pgl, "DEFAULT_LOG_DIR", self.log_dir))
            stack.enter_context(patch("position_guardian_loop.time.sleep", side_effect=KeyboardInterrupt))
            if cycle is not None:
                stack.enter_context(patch.object(pgl, "run_cycle", cycle))
            return capture(pgl.main, argv)

    def test_default_interval_is_60_and_passes_the_gate(self):
        self.assertEqual(pgl.DEFAULT_INTERVAL_SECONDS, 60)
        code, _, err = self.run_loop(["--env", "testnet"])
        self.assertEqual(code, 0)
        self.assertNotIn("WARNING", err)
        with open(os.path.join(self.log_dir, pgl.STATE_FILE_NAME), "r", encoding="utf-8") as f:
            state = json.load(f)
        self.assertEqual((state["mode"], state["interval_seconds"]), ("loop", 60))
        with patch("execute_futures_trade._workspace_dir", return_value=self.ws):
            self.assertTrue(eft.check_guardian_alive("testnet")[0])

    def test_interval_above_limit_warns_and_keeps_running(self):
        code, _, err = self.run_loop(["--interval", "300", "--env", "testnet"])
        self.assertEqual(code, 0)
        warnings = [line for line in err.splitlines() if "WARNING" in line]
        self.assertEqual(len(warnings), 1, err)
        self.assertIn("300s exceeds 120s", warnings[0])
        self.assertIn("resting", warnings[0])
        with open(os.path.join(self.log_dir, pgl.STATE_FILE_NAME), "r", encoding="utf-8") as f:
            self.assertEqual(json.load(f)["interval_seconds"], 300)
        _, _, err = self.run_loop(["--interval", "120", "--env", "testnet"])
        self.assertNotIn("WARNING", err)
        _, _, err = self.run_loop(["--once", "--interval", "300", "--env", "testnet"])
        self.assertNotIn("WARNING", err)

    def test_log_file_tees_and_rotates(self):
        log_path = os.path.join(self.ws, "out", "guardian.log")
        with patch.object(pgl, "LOG_FILE_MAX_BYTES", 300):
            code, out, _ = self.run_loop(["--once", "--json", "--env", "testnet", "--log-file", log_path])
        self.assertEqual(code, 0)
        self.assertIn('"mode": "once"', out, "still printed to stdout")
        self.assertTrue(os.path.exists(log_path))
        self.assertTrue(os.path.exists(log_path + ".1"), "rotated with a small maxBytes")
        self.assertFalse(os.path.exists(log_path + ".4"), "3 backups at most")
        logged = ""
        for suffix in ("", ".1", ".2", ".3"):
            if os.path.exists(log_path + suffix):
                with open(log_path + suffix, "r", encoding="utf-8") as f:
                    logged += f.read()
        self.assertIn('"schema_version": 1', logged)
        self.assertNotIsInstance(sys.stdout, pgl._TeeStream)
        self.assertNotIsInstance(sys.stderr, pgl._TeeStream)

    def test_log_file_captures_stderr_and_relative_path_uses_repo_root(self):
        with patch.object(pgl, "BASE_DIR", self.ws):
            self.run_loop(["--interval", "300", "--env", "testnet", "--log-file", "logs/guardian.log"])
        with open(os.path.join(self.ws, "logs", "guardian.log"), "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn("WARNING --interval 300s", content)
        self.assertIn("GUARDIAN TESTNET", content)

    def test_without_log_file_nothing_is_written(self):
        with patch.object(pgl, "BASE_DIR", self.ws):
            self.run_loop(["--once", "--env", "testnet"])
        self.assertFalse(os.path.exists(os.path.join(self.ws, "logs", "guardian.log")))

    @unittest.skipIf(fcntl is None, "fcntl not available")
    def test_second_loop_exits_0_while_the_lock_is_held_and_once_ignores_it(self):
        os.makedirs(self.log_dir, exist_ok=True)
        holder = open(os.path.join(self.log_dir, pgl.lock_file_name("testnet")), "a+")
        self.addCleanup(holder.close)
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        cycle = MagicMock(side_effect=AssertionError("the second loop must not run a cycle"))
        code, out, _ = self.run_loop(["--interval", "60", "--env", "testnet"], cycle=cycle)
        self.assertEqual(code, 0)
        self.assertIn("another guardian loop for testnet is already running (holder details unavailable); exiting",
                      out)
        cycle.assert_not_called()
        code, out, _ = self.run_loop(["--once", "--env", "testnet"])
        self.assertEqual(code, 0, out)
        self.assertIn("GUARDIAN TESTNET", out)
        code, out, _ = self.run_loop(["--interval", "60", "--dry-run", "--env", "testnet"])
        self.assertEqual(code, 0, out)
        self.assertIn("DRY RUN", out)
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        code, out, _ = self.run_loop(["--interval", "60", "--env", "testnet"])
        self.assertNotIn("already running", out)
        self.assertIn("GUARDIAN TESTNET", out)

    def fake_fcntl(self, error):
        fake = MagicMock(LOCK_EX=2, LOCK_NB=4, LOCK_UN=8)
        fake.flock.side_effect = error
        return patch.object(pgl, "fcntl", fake)

    def test_lock_error_other_than_held_runs_the_loop_unlocked(self):
        for code_ in (errno.ENOLCK, errno.EOPNOTSUPP, errno.EIO):
            with self.fake_fcntl(OSError(code_, os.strerror(code_))):
                code, out, err = self.run_loop(["--interval", "60", "--env", "testnet"])
            self.assertEqual(code, 0)
            self.assertNotIn("already running", out)
            self.assertIn("GUARDIAN TESTNET", out, "the cycle ran")
            self.assertIn("running without the single-instance lock", err)

    def test_lock_file_open_failure_runs_the_loop_unlocked(self):
        not_a_dir = os.path.join(self.ws, "file")
        with open(not_a_dir, "w", encoding="utf-8") as f:
            f.write("x")
        with patch.object(pgl, "run_cycle", MagicMock(return_value={"cycle_ok": True})), \
             patch.object(pgl, "format_state", return_value="CYCLE RAN"), \
             patch.object(pgl, "DEFAULT_LOG_DIR", os.path.join(not_a_dir, "logs")), \
             patch("position_guardian_loop.time.sleep", side_effect=KeyboardInterrupt):
            code, out, err = capture(pgl.main, ["--interval", "60", "--env", "testnet"])
        self.assertEqual(code, 0)
        self.assertIn("CYCLE RAN", out)
        self.assertIn("cannot open guardian_loop.testnet.lock", err)

    def test_held_lock_errno_exits_0_with_json(self):
        for code_ in (errno.EWOULDBLOCK, errno.EAGAIN):
            cycle = MagicMock(side_effect=AssertionError("no cycle"))
            with self.fake_fcntl(BlockingIOError(code_, "busy")):
                code, out, _ = self.run_loop(["--interval", "60", "--env", "testnet", "--json"], cycle=cycle)
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out), {"success": True, "already_running": True, "env": "testnet",
                                               "holder": None,
                                               "message": "another guardian loop for testnet is already running "
                                                          "(holder details unavailable); exiting"})
            cycle.assert_not_called()

    def test_log_rollover_failure_never_stops_the_cycle(self):
        log_path = os.path.join(self.ws, "out", "guardian.log")
        with patch.object(pgl, "LOG_FILE_MAX_BYTES", 50), \
             patch.object(pgl._GuardianLogHandler, "doRollover", side_effect=PermissionError(13, "file in use")):
            code, out, err = self.run_loop(["--once", "--json", "--env", "testnet", "--log-file", log_path])
        self.assertEqual(code, 0)
        self.assertIn('"mode": "once"', out, "stdout still gets the cycle output")
        self.assertEqual(err.count("--log-file write failed"), 1, err)
        self.assertIn("PermissionError", err)
        self.assertNotIsInstance(sys.stderr, pgl._TeeStream)

    def test_log_handler_emit_failure_never_stops_the_loop(self):
        log_path = os.path.join(self.ws, "out", "guardian.log")
        with patch.object(pgl._GuardianLogHandler, "emit", side_effect=OSError(28, "No space left on device")):
            code, out, err = self.run_loop(["--interval", "300", "--env", "testnet", "--log-file", log_path])
        self.assertEqual(code, 0)
        self.assertIn("GUARDIAN TESTNET", out)
        self.assertIn("WARNING --interval 300s", err)

    @unittest.skipIf(fcntl is None, "fcntl not available")
    def test_loop_releases_the_lock_on_exit(self):
        self.run_loop(["--interval", "60", "--env", "testnet"])
        with open(os.path.join(self.log_dir, pgl.lock_file_name("testnet")), "a+") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)  # raises if still held
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


class TestDoctorGuardianCheck(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp()
        p = patch("execute_futures_trade._workspace_dir", return_value=self.ws)
        p.start()
        self.addCleanup(p.stop)

    def test_warn_with_install_hint_when_not_alive(self):
        with patch("subprocess.run", side_effect=AssertionError("the guardian check runs no subprocess")), \
             patch("utils.dossier_provenance._is_wsl", return_value=True):
            level, msg = trading_doctor.check_guardian_service("prod")
            self.assertEqual(level, "warn")
            self.assertIn("python3 scripts/install_guardian_service.py --install --env prod", msg)
            self.assertIn("MARKET entries do not need the guardian", msg)
            self.assertIn("PROD resting STOP_MARKET/LIMIT entries are rejected", msg)
            write_guardian_state(self.ws, env="prod", age=1, interval_seconds=300)
            self.assertEqual(trading_doctor.check_guardian_service("prod")[0], "warn")

    def test_testnet_wording_and_non_wsl_hint(self):
        with patch("utils.dossier_provenance._is_wsl", return_value=False):
            level, msg = trading_doctor.check_guardian_service("testnet")
        self.assertEqual(level, "warn")
        self.assertNotIn("PROD", msg)
        self.assertNotIn("rejected", msg)
        self.assertNotIn("install_guardian_service", msg)
        self.assertIn("python3 scripts/loops/position_guardian_loop.py --interval 60 --env testnet", msg)
        with patch("utils.dossier_provenance._is_wsl", return_value=False):
            msg = trading_doctor.check_guardian_service("prod")[1]
        self.assertIn("PROD resting STOP_MARKET/LIMIT entries are rejected", msg)
        self.assertIn("position_guardian_loop.py --interval 60 --env prod", msg)
        self.assertNotIn("install_guardian_service", msg)

    def test_resting_entry_rejection_mentions_the_installer(self):
        ok, msg = eft.check_resting_entry_gates("BTCUSDT", "prod")
        self.assertFalse(ok)
        self.assertIn("python3 scripts/install_guardian_service.py --install --env prod", msg)
        self.assertIn("python3 scripts/loops/position_guardian_loop.py --interval 60 --env prod", msg)

    def test_ok_when_alive(self):
        write_guardian_state(self.ws, env="prod", age=1)
        level, msg = trading_doctor.check_guardian_service("prod")
        self.assertEqual(level, "ok")
        self.assertIn("guardian alive", msg)

    def test_doctor_warns_without_changing_the_exit_code(self):
        resp = MagicMock()
        resp.read.return_value = json.dumps({"serverTime": int(time.time() * 1000)}).encode()
        profile = {"profile_completed": True, "risk_pct_equity": 0.005, "yolo_slot_enabled": False}
        with patch("execute_futures_trade.load_env", return_value={"LIVE_TRADING_ARMED": "true"}), \
             patch("execute_futures_trade.get_client_config", return_value=("key12345678", "sec12345678", "http://x")), \
             patch("urllib.request.urlopen") as mock_urlopen, \
             patch("execute_futures_trade.send_signed_request",
                   side_effect=lambda method, endpoint, params=None, target_env=None, **kw: [
                       {"asset": "USDT", "balance": "1000.0", "availableBalance": "1000.0"}]
                   if endpoint == "/fapi/v2/balance" else []), \
             patch("user_profile.load_user_profile", return_value=profile), \
             patch("trading_doctor.check_pretool_hook", return_value={"ok": True, "critical": [], "warnings": [], "info": []}), \
             patch("shadow_tracker.calculate_efficacy_metrics", side_effect=RuntimeError("skip")), \
             patch.object(sss, "STATE_FILE", os.path.join(self.ws, "session_state.json")), \
             patch("sync_session_state.sync_session_state", MagicMock(return_value={"is_valid": True})), \
             patch("utils.dossier_provenance._is_wsl", return_value=True):
            mock_urlopen.return_value.__enter__.return_value = resp
            code, out, _ = capture(trading_doctor.run_doctor, target_env="testnet")
        self.assertEqual(code, 0, out)
        self.assertIn("⚠️  [GUARDIAN] Position guardian loop not alive", out)
        self.assertIn("python3 scripts/install_guardian_service.py --install --env testnet", out)
        self.assertNotIn("SYSTEM DISABLED", out)


class TestOnboardingGuardianOffer(unittest.TestCase):
    ANSWERS = ["1", "1", "1", "2", "1"]

    def run_onboarding(self, answers, wsl=True):
        tmp = tempfile.mkdtemp()
        install = MagicMock(return_value=0)
        with patch("user_profile.PROFILE_FILE", os.path.join(tmp, "user_profile.json")), \
             patch("user_profile.CONFIG_DIR", tmp), \
             patch("builtins.input", side_effect=list(answers)) as inp, \
             patch.object(isg, "is_wsl", return_value=wsl), \
             patch.object(isg, "install", install):
            capture(up.interactive_terminal_onboarding)
            with open(os.path.join(tmp, "user_profile.json"), "r", encoding="utf-8") as f:
                saved = json.load(f)
        return install, inp, saved

    def test_yes_calls_the_installer(self):
        install, inp, saved = self.run_onboarding(self.ANSWERS + ["y"])
        install.assert_called_once_with()
        self.assertEqual(inp.call_count, 6)
        self.assertIn("Windows background task", inp.call_args_list[-1][0][0])
        self.assertTrue(saved["profile_completed"])
        self.assertFalse([k for k in saved if "guardian" in k.lower()], "no new profile key")

    def test_no_does_not_call_the_installer(self):
        for answer in ("n", "", "N"):
            install, inp, saved = self.run_onboarding(self.ANSWERS + [answer])
            install.assert_not_called()
            self.assertEqual(inp.call_count, 6)
            self.assertTrue(saved["profile_completed"])

    def test_not_asked_outside_wsl(self):
        install, inp, _ = self.run_onboarding(self.ANSWERS, wsl=False)
        install.assert_not_called()
        self.assertEqual(inp.call_count, 5)

    def test_installer_failure_keeps_the_profile(self):
        tmp = tempfile.mkdtemp()
        with patch("user_profile.PROFILE_FILE", os.path.join(tmp, "user_profile.json")), \
             patch("user_profile.CONFIG_DIR", tmp), \
             patch("builtins.input", side_effect=self.ANSWERS + ["yes"]), \
             patch.object(isg, "is_wsl", return_value=True), \
             patch.object(isg, "install", side_effect=RuntimeError("schtasks missing")):
            _, out, _ = capture(up.interactive_terminal_onboarding)
            self.assertTrue(os.path.exists(os.path.join(tmp, "user_profile.json")))
        self.assertIn("Guardian install did not complete", out)


if __name__ == "__main__":
    unittest.main()
