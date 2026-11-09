"""Windows tests for agent_resume.py. No herdr-core is needed: `hc` is replaced by a fake
and the process helpers are checked against real processes of this test run."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

if sys.platform != "win32":
    raise unittest.SkipTest("Windows-only tests")

spec = importlib.util.spec_from_file_location("agent_resume", Path(__file__).parents[1] / "mods/agent-resume/agent_resume.py")
ar = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ar)

BOOT = "winboot:1"
SID = "4f1c2a9e-7b3d-4e8a-9c51-2d6f0a8b3e17"
SID2 = "019a2b7c-3d4e-7f10-8a2b-c3d4e5f60718"
CWD = "C:\\Users\\Dev\\project"


def label(agent="claude", session=SID, shell=100, boot=BOOT, at=1, cwd=CWD, pid=200, args=None, transcript=None):
    value = {"agent": agent, "session": session, "cwd": cwd,
             "transcript": transcript or f"C:\\Users\\Dev\\.claude\\projects\\p\\{session}.jsonl",
             "pid": pid, "shell": shell, "boot": boot, "at": at}
    if args is not None:
        value["args"] = args
    return value


def terminal(address="t1", pid=300, name="pwsh.exe", lbl=None, state="live", where="api / 1", fg_pid=None):
    labels = {"where": where}
    if lbl is not None:
        labels[ar.LABEL] = json.dumps(lbl)
    return {"address": address, "state": state, "pid": pid, "labels": labels,
            "foreground": {"pid": pid if fg_pid is None else fg_pid, "name": name}}


def run_plan(terminals, boot=BOOT, **kw):
    return ar.plan(terminals, boot, folder_ok=kw.pop("folder_ok", lambda p: True),
                   file_ok=kw.pop("file_ok", lambda p: True), **kw)


class Sandbox(unittest.TestCase):
    """Every test gets its own app home and runtime folder, never the real ones."""

    def setUp(self):
        # A lock file held by this very process can't be deleted on Windows; that's not a failure.
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"HERDR_APP_HOME": os.path.join(self.tmp.name, "app"),
                                               "AGENT_RESUME_RUNTIME": os.path.join(self.tmp.name, "run")})
        patcher.start()
        self.addCleanup(patcher.stop)


class ProcessTests(unittest.TestCase):
    def test_proc_reads_this_process(self):
        info = ar.proc(os.getpid())
        self.assertIn("python", info["names"])  # ".exe" and case are normalised away
        self.assertEqual(info["ppid"], os.getppid())
        self.assertIsInstance(info["start"], int)

    def test_gone_and_invalid_pids(self):
        for pid in (None, 0, -5, "12", 2 ** 30):
            self.assertIsNone(ar.proc(pid), pid)
        self.assertFalse(ar.alive(2 ** 30))
        self.assertTrue(ar.alive(os.getpid()))

    def test_names_normalise_windows_images(self):
        self.assertEqual(ar._names("claude.exe"), {"claude"})
        self.assertEqual(ar._names("C:\\Users\\x\\.local\\bin\\Claude.EXE"), {"claude"})
        self.assertEqual(ar._names("pwsh.exe", "codex.cmd", "node.exe"), {"pwsh", "codex", "node"})
        self.assertEqual(ar._names("", None, "  "), set())

    def test_start_time_tells_runs_of_a_pid_apart_and_is_stable(self):
        self.assertEqual(ar.process_start(os.getpid()), ar.process_start(os.getpid()))
        self.assertIsNone(ar.process_start(2 ** 30))
        self.assertGreater(ar.shell_age(os.getpid()), 0)

    def test_boot_id_is_stable_text(self):
        first = ar.boot_id()
        self.assertTrue(first.startswith("winboot:"))
        self.assertEqual(first, ar.boot_id())

    def test_argv_keeps_spaces_and_chinese(self):
        folder = Path(tempfile.mkdtemp(prefix="測試 資料夾 "))
        self.addCleanup(lambda: __import__("shutil").rmtree(folder, ignore_errors=True))
        script = folder / "sleeper.py"
        script.write_text("import time; time.sleep(30)\n", encoding="utf-8")
        child = subprocess.Popen([sys.executable, str(script), "--model", "a b", "中文"])
        try:
            time.sleep(0.5)
            argv = ar.proc_argv(child.pid)
            self.assertEqual(argv[1:], [str(script), "--model", "a b", "中文"])
        finally:
            child.kill()
            child.wait()

    def test_wait_exit_returns_when_the_process_ends(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(1)"])
        started = time.monotonic()
        ar.wait_exit(child.pid)
        self.assertGreater(time.monotonic() - started, 0.3)
        self.assertFalse(ar.alive(child.pid))
        child.wait()

    def test_wait_exit_on_a_gone_pid_returns_at_once(self):
        started = time.monotonic()
        ar.wait_exit(2 ** 30)
        self.assertLess(time.monotonic() - started, 3)

    def test_descendants_and_idle_shell(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            time.sleep(0.3)
            self.assertIn("python", ar.descendants(os.getpid()))
            busy = terminal(pid=os.getpid(), name="pwsh.exe")
            self.assertFalse(ar.at_shell(busy))
        finally:
            child.kill()
            child.wait()
        idle = terminal(pid=2 ** 30, name="pwsh.exe")
        self.assertTrue(ar.at_shell(idle))

    def test_console_helpers_do_not_make_a_shell_busy(self):
        table = {10: (1, "pwsh.exe"), 11: (10, "conhost.exe"), 12: (10, "OpenConsole.exe")}
        with mock.patch.object(ar.winproc, "snapshot", return_value=table):
            self.assertTrue(ar.at_shell(terminal(pid=10)))
        table[13] = (10, "claude.exe")
        with mock.patch.object(ar.winproc, "snapshot", return_value=table):
            self.assertFalse(ar.at_shell(terminal(pid=10)))

    def test_placement_through_a_node_wrapper(self):
        # pwsh(10) -> node(11) -> codex(12), like the npm shim
        table = {10: (1, "pwsh.exe"), 11: (10, "node.exe"), 12: (11, "codex.exe")}
        with mock.patch.object(ar.winproc, "snapshot", return_value=table), \
                mock.patch.object(ar.winproc, "start_time", return_value=1):
            self.assertEqual(ar.find_agent("codex", 12), 12)
            self.assertEqual(ar.placement(12, 10), "direct")

    def test_an_agent_started_by_another_agent_is_nested(self):
        table = {10: (1, "pwsh.exe"), 11: (10, "claude.exe"), 12: (11, "bash.exe"), 13: (12, "codex.exe")}
        with mock.patch.object(ar.winproc, "snapshot", return_value=table), \
                mock.patch.object(ar.winproc, "start_time", return_value=1):
            self.assertEqual(ar.placement(13, 10), "nested")

    def test_a_job_outside_the_shell_is_outside(self):
        table = {10: (1, "pwsh.exe"), 20: (2, "wt.exe"), 21: (20, "claude.exe")}
        with mock.patch.object(ar.winproc, "snapshot", return_value=table), \
                mock.patch.object(ar.winproc, "start_time", return_value=1):
            self.assertEqual(ar.placement(21, 10), "outside")


class LabelTests(unittest.TestCase):
    def test_reads_a_windows_label_with_chinese_and_spaces(self):
        saved = label(cwd="C:\\Users\\王小明\\我的 專案")
        read = ar.read_label({ar.LABEL: json.dumps(saved, ensure_ascii=False)})
        self.assertEqual(read["cwd"], saved["cwd"])

    def test_unc_and_drive_paths_are_accepted(self):
        for cwd in ("D:\\work", "D:/work", "\\\\nas\\share\\proj"):
            self.assertIsNotNone(ar.read_label({ar.LABEL: json.dumps(label(cwd=cwd))}), cwd)

    def test_posix_and_relative_paths_are_rejected(self):
        for cwd in ("/work", "work\\x", "\\rooted", "C:relative", ""):
            self.assertIsNone(ar.read_label({ar.LABEL: json.dumps(label(cwd=cwd))}), cwd)

    def test_shell_start_must_be_a_positive_integer(self):
        for bad in (0, -1, "1", True, 1.5):
            self.assertIsNone(ar.read_label({ar.LABEL: json.dumps({**label(), "shell_start": bad})}), bad)
        self.assertIsNotNone(ar.read_label({ar.LABEL: json.dumps({**label(), "shell_start": 134000000000000000})}))

    def test_unsupported_agents_are_quiet_when_real_and_reported_when_not(self):
        self.assertEqual(ar.parse_label({ar.LABEL: json.dumps(label(agent="grok"))}), (None, None))
        self.assertIn("unsupported", ar.parse_label({ar.LABEL: json.dumps(label(agent="vim"))})[1])

    def test_broken_labels(self):
        for raw in ("not json", json.dumps([1]), json.dumps(label(session="x; calc")), json.dumps(label(session="short"))):
            self.assertIsNone(ar.read_label({ar.LABEL: raw}), raw)

    def test_a_label_can_only_carry_kept_non_dangerous_flags(self):
        read = ar.read_label({ar.LABEL: json.dumps(label(args=["--model", "opus", "--dangerously-skip-permissions",
                                                               "--resume", "other", "calc.exe"]))})
        self.assertEqual(read["args"], ["--model", "opus"])


class LostTests(unittest.TestCase):
    def test_same_shell_same_boot_is_not_lost(self):
        self.assertFalse(ar.lost({"pid": 100}, label(shell=100), BOOT))

    def test_another_shell_or_boot_is_lost(self):
        self.assertTrue(ar.lost({"pid": 101}, label(shell=100), BOOT))
        self.assertTrue(ar.lost({"pid": 100}, label(shell=100, boot="winboot:0"), BOOT))

    def test_a_reused_pid_on_the_same_boot_is_lost_by_its_start_time(self):
        # Fast Startup keeps the boot time and Windows hands pids out again.
        mine = ar.process_start(os.getpid())
        saved = label(shell=os.getpid())
        self.assertFalse(ar.lost({"pid": os.getpid()}, {**saved, "shell_start": mine}, BOOT))
        self.assertTrue(ar.lost({"pid": os.getpid()}, {**saved, "shell_start": mine - 10 ** 9}, BOOT))

    def test_an_unreadable_pid_never_counts_as_a_different_run(self):
        self.assertFalse(ar.lost({"pid": 2 ** 30}, {**label(shell=2 ** 30), "shell_start": 5}, BOOT))


class QuotingTests(unittest.TestCase):
    HOSTILE = ["a'b", "it's", "\u2018x\u2019", "$(calc)", "`calc`", "a;b", "a|b", "a&b", "a b", "100%", "x$env:PATH",
               "中文 路徑", "a\\b", "C:\\Program Files\\x"]

    def test_ps_quote_keeps_everything_literal(self):
        self.assertEqual(ar.ps_quote("plain"), "'plain'")
        self.assertEqual(ar.ps_quote("it's"), "'it''s'")
        self.assertEqual(ar.ps_quote("\u2018x\u2019"), "'\u2018\u2018x\u2019\u2019'")
        self.assertEqual(ar.ps_quote("$(calc) `x` ; | &"), "'$(calc) `x` ; | &'")

    def test_ps_quote_refuses_what_cannot_be_typed(self):
        for bad in ("a\nb", "a\rb", "a\x00b", "a\x1bb", "a\u2028b", "a\x85b"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                ar.ps_quote(bad)

    def test_a_quoted_ps_string_survives_a_real_powershell(self):
        """The strongest check: PowerShell itself reads each hostile value back unchanged."""
        pwsh = ar.shutil.which("pwsh") or ar.shutil.which("powershell")
        if not pwsh:
            self.skipTest("no PowerShell")
        for value in self.HOSTILE:
            script = f"[Console]::OutputEncoding=[Text.Encoding]::UTF8; Write-Output {ar.ps_quote(value)}"
            out = subprocess.run([pwsh, "-NoProfile", "-NonInteractive", "-Command", script],
                                 capture_output=True, encoding="utf-8").stdout.rstrip("\r\n")
            self.assertEqual(out, value, value)

    def test_cmd_quote(self):
        self.assertEqual(ar.cmd_quote("C:\\work"), "C:\\work")
        self.assertEqual(ar.cmd_quote("C:\\my work"), '"C:\\my work"')
        for bad in ("a&b", "a|b", "a<b", "a>b", "a^b", "%PATH%", "a!b", 'a"b', "a\nb"):
            with self.assertRaises(ValueError, msg=bad):
                ar.cmd_quote(bad)

    def test_powershell_command(self):
        typed = ar.windows_command(label(cwd="C:\\Users\\王小明\\it's here", args=["--model", "opus"]), "ps")
        self.assertEqual(typed, "Set-Location -LiteralPath 'C:\\Users\\王小明\\it''s here' -ErrorAction Stop; "
                                f"if ($?) {{ claude '--resume' '{SID}' '--model' 'opus' }}")

    def test_codex_command(self):
        typed = ar.windows_command(label(agent="codex", args=["-m", "gpt-5", "-s", "workspace-write"]), "ps")
        self.assertTrue(typed.endswith(f"{{ codex 'resume' '--no-daemon' '{SID}' '-m' 'gpt-5' '-s' 'workspace-write' }}"), typed)

    def test_cmd_command(self):
        typed = ar.windows_command(label(cwd="C:\\my work", args=["--model", "opus"]), "cmd")
        self.assertEqual(typed, f'cd /d "C:\\my work" && claude --resume {SID} --model opus')

    def test_cmd_refuses_unsafe_folders_and_unc(self):
        for cwd in ("C:\\a&b", "C:\\50%", "C:\\a^b", "\\\\nas\\share"):
            self.assertIsNone(ar.windows_command(label(cwd=cwd), "cmd"), cwd)
        self.assertIsNotNone(ar.windows_command(label(cwd="\\\\nas\\share"), "ps"))

    def test_ps_refuses_control_characters_in_the_folder(self):
        self.assertIsNone(ar.windows_command(label(cwd="C:\\a\nb"), "ps"))

    def test_flags_with_a_double_quote_are_left_out_and_reported(self):
        saved = label(args=["--model", "opus", "--append-system-prompt", 'say "hi"'])
        kept, omitted = ar.flag_report(saved, "ps")
        self.assertEqual(ar.flat(kept), ["--model", "opus"])
        self.assertEqual(omitted, ["--append-system-prompt"])
        self.assertNotIn("say", ar.windows_command(saved, "ps"))

    def test_the_session_id_cannot_carry_a_command(self):
        for session in ("x'; calc; '", "a b c d e f g h", "../../etc/x1234567", "id\nsecond-line"):
            self.assertFalse(ar.valid_session(session), session)

    def test_shell_kinds(self):
        self.assertEqual(ar.shell_kind(terminal(name="pwsh.exe")), "ps")
        self.assertEqual(ar.shell_kind(terminal(name="powershell.exe")), "ps")
        self.assertEqual(ar.shell_kind(terminal(name="CMD.EXE")), "cmd")
        self.assertIsNone(ar.shell_kind(terminal(name="bash.exe")))
        self.assertIsNone(ar.shell_kind(terminal(name="wsl.exe")))


class DangerousFlagTests(unittest.TestCase):
    def kept(self, agent, args):
        return ar.flat(ar.launch_flags(agent, args))

    def test_claude_bypass_flags_are_never_inherited(self):
        self.assertEqual(self.kept("claude", ["--dangerously-skip-permissions", "--model", "opus"]), ["--model", "opus"])
        self.assertEqual(self.kept("claude", ["--allow-dangerously-skip-permissions"]), [])
        self.assertEqual(self.kept("claude", ["--permission-mode", "bypassPermissions"]), [])
        self.assertEqual(self.kept("claude", ["--permission-mode=bypassPermissions"]), [])
        self.assertEqual(self.kept("claude", ["--permission-mode", "plan"]), ["--permission-mode", "plan"])

    def test_codex_bypass_flags_are_never_inherited(self):
        for args in (["--dangerously-bypass-approvals-and-sandbox"], ["-s", "danger-full-access"],
                     ["--sandbox=danger-full-access"], ["-a", "never"], ["--ask-for-approval", "never"],
                     ["-c", "sandbox_mode=danger-full-access"], ["-c", 'approval_policy="never"'],
                     ["--config", "sandbox_mode = 'danger-full-access'"]):
            self.assertEqual(self.kept("codex", args), [], args)

    def test_codex_safe_flags_stay(self):
        self.assertEqual(self.kept("codex", ["-m", "gpt-5", "-s", "workspace-write", "-a", "on-request"]),
                         ["-m", "gpt-5", "-s", "workspace-write", "-a", "on-request"])

    def test_the_command_reports_what_it_left_out(self):
        saved = label(args=["--dangerously-skip-permissions", "--model", "opus"])
        saved["args"] = ["--model", "opus"]  # already filtered when parsed
        raw = label()
        raw["args"] = ["--dangerously-skip-permissions", "--model", "opus"]
        kept, omitted = ar.flag_report(raw, "ps")
        self.assertEqual(omitted, ["--dangerously-skip-permissions"])
        self.assertNotIn("dangerously", ar.windows_command(raw, "ps"))

    def test_launch_flags_never_take_a_prompt_or_session_picker(self):
        self.assertEqual(self.kept("claude", ["--resume", "abc", "-p", "do it", "fix the bug", "--model", "opus"]),
                         ["--model", "opus"])


class PlanTests(unittest.TestCase):
    def test_resumes_a_lost_agent_at_an_idle_shell(self):
        todo, skipped = run_plan([terminal(lbl=label(shell=100), pid=300)])
        self.assertEqual([t["terminal"] for t in todo], ["t1"])
        self.assertEqual(skipped, [])
        self.assertIn("Set-Location", todo[0]["command"])
        self.assertIn(SID, todo[0]["command"])
        self.assertEqual(todo[0]["label_at"], 1)

    def test_a_running_agent_is_not_started_twice(self):
        todo, skipped = run_plan([terminal(lbl=label(shell=300), pid=300)])
        self.assertEqual((todo, skipped), ([], []))

    def test_an_agent_still_under_a_new_shell_is_not_repeated(self):
        table = {300: (1, "pwsh.exe"), 301: (300, "claude.exe")}
        with mock.patch.object(ar.winproc, "snapshot", return_value=table):
            todo, skipped = run_plan([terminal(lbl=label(shell=100), pid=300)])
        self.assertEqual(todo, [])
        self.assertEqual([s["reason"] for s in skipped], [ar.BY_SHELL])

    def test_something_else_running_in_the_shell_is_left_alone(self):
        table = {300: (1, "pwsh.exe"), 301: (300, "python.exe")}
        with mock.patch.object(ar.winproc, "snapshot", return_value=table):
            todo, skipped = run_plan([terminal(lbl=label(shell=100), pid=300)])
        self.assertEqual(todo, [])
        self.assertEqual([s["reason"] for s in skipped], ["something else is running there"])

    def test_unsupported_shells_are_skipped_not_typed_into(self):
        todo, skipped = run_plan([terminal(lbl=label(shell=100), name="bash.exe")])
        self.assertEqual(todo, [])
        self.assertIn("isn't PowerShell or cmd", skipped[0]["reason"])

    def test_missing_folder_and_transcript(self):
        _, skipped = run_plan([terminal(lbl=label(shell=100))], folder_ok=lambda p: False)
        self.assertEqual(skipped[0]["reason"], "its folder is gone")
        _, skipped = run_plan([terminal(lbl=label(shell=100))], file_ok=lambda p: False)
        self.assertEqual(skipped[0]["reason"], "no saved conversation to resume")

    def test_a_folder_that_cannot_be_typed_into_cmd_is_skipped(self):
        todo, skipped = run_plan([terminal(lbl=label(shell=100, cwd="C:\\a&b"), name="cmd.exe")])
        self.assertEqual(todo, [])
        self.assertEqual(skipped[0]["reason"], "its folder name can't be typed safely")

    def test_the_same_session_resumes_in_one_terminal_only_newest_first(self):
        old = terminal("t1", lbl=label(shell=100, at=1))
        new = terminal("t2", pid=301, lbl=label(shell=101, at=2))
        todo, skipped = run_plan([old, new])
        self.assertEqual([t["terminal"] for t in todo], ["t2"])
        self.assertEqual([s["reason"] for s in skipped], ["resumed in another terminal"])

    def test_claude_and_codex_with_the_same_id_are_different_sessions(self):
        a = terminal("t1", lbl=label(agent="claude", shell=100))
        b = terminal("t2", pid=301, lbl=label(agent="codex", shell=101))
        todo, _ = run_plan([a, b])
        self.assertEqual(sorted(t["agent"] for t in todo), ["claude", "codex"])

    def test_a_terminal_that_is_not_started_yet_is_waited_for(self):
        _, skipped = run_plan([terminal(pid=None, lbl=label(shell=100))])
        self.assertEqual(skipped[0]["reason"], ar.NOT_STARTED)

    def test_terminals_without_a_label_and_dead_ones_are_ignored(self):
        self.assertEqual(run_plan([terminal(), terminal(state="finished", lbl=label())]), ([], []))

    def test_a_bad_label_is_reported_without_blocking_the_others(self):
        bad = terminal("t1", lbl=None)
        bad["labels"][ar.LABEL] = "not json"
        good = terminal("t2", pid=301, lbl=label(shell=100))
        todo, skipped = run_plan([bad, good])
        self.assertEqual([t["terminal"] for t in todo], ["t2"])
        self.assertIn("malformed JSON", skipped[0]["reason"])

    def test_omitted_flags_are_surfaced(self):
        raw = label(shell=100)
        raw["args"] = ["--model", "opus"]
        item = terminal(lbl=raw)
        # a hand-written label can still hold a dangerous flag: it must not reach the command
        item["labels"][ar.LABEL] = json.dumps({**raw, "args": ["--dangerously-skip-permissions", "--model", "opus"]})
        todo, _ = run_plan([item])
        self.assertNotIn("dangerously", todo[0]["command"])


class StateTests(Sandbox):
    def item(self, at=1, agent="claude", session=SID):
        return {"agent": agent, "session": session, "terminal": "t1", "label_at": at}

    def test_config_defaults_to_off_and_toggles(self):
        self.assertFalse(ar.load_config()["auto"])
        self.assertTrue(ar.set_auto(True)["auto"])
        self.assertTrue(ar.load_config()["auto"])
        self.assertFalse(ar.set_auto(False)["auto"])

    def test_a_damaged_config_means_off(self):
        os.makedirs(ar.data_dir(), exist_ok=True)
        Path(ar.config_path()).write_text("{not json", encoding="utf-8")
        self.assertEqual(ar.load_config(), {"auto": False, "interval": ar.AUTO_INTERVAL})
        Path(ar.config_path()).write_text(json.dumps({"auto": "yes", "interval": 0}), encoding="utf-8")
        self.assertEqual(ar.load_config(), {"auto": False, "interval": ar.AUTO_INTERVAL})

    def test_automatic_attempt_is_once_per_noted_session(self):
        self.assertIsNone(ar.auto_veto(label(at=5)))
        ar.record_attempt(self.item(at=5), "auto", "failed", "exited right away")
        self.assertIn("already tried", ar.auto_veto(label(at=5)))
        # the hook noted the session again after a real resume or a new start: a fresh chance
        self.assertIsNone(ar.auto_veto(label(at=6)))

    def test_a_manual_attempt_never_blocks_the_automatic_pass_and_vice_versa(self):
        ar.record_attempt(self.item(at=5), "manual", "failed", "x")
        self.assertIsNone(ar.auto_veto(label(at=5)))
        ar.record_attempt(self.item(at=5), "auto", "typed")
        _, skipped = run_plan([terminal(lbl=label(shell=100, at=5))], veto=ar.auto_veto)
        self.assertIn("already tried", skipped[0]["reason"])
        todo, _ = run_plan([terminal(lbl=label(shell=100, at=5))])  # the manual pass ignores it
        self.assertEqual(len(todo), 1)

    def test_failures_are_kept_with_their_history(self):
        for n in range(15):
            ar.record_attempt(self.item(), "manual", "failed", f"try {n}")
        entry = ar.load_state()["sessions"][ar.session_key("claude", SID)]
        self.assertEqual(len(entry["attempts"]), ar.ATTEMPTS_KEPT)
        self.assertEqual(entry["attempts"][-1]["detail"], "try 14")
        self.assertEqual(entry["last"], "failed")

    def test_old_sessions_are_forgotten(self):
        ar.record_attempt(self.item(), "manual", "typed")
        state = ar.load_state()
        state["sessions"][ar.session_key("claude", SID)]["updated"] = 1
        ar._write_json(ar.state_path(), state)
        ar.record_attempt(self.item(session=SID2), "manual", "typed")
        self.assertEqual(list(ar.load_state()["sessions"]), [ar.session_key("claude", SID2)])

    def test_the_log_is_utf8_json_lines(self):
        ar.log("test", where="專案 / Claude")
        entries = ar.tail_log(5)
        self.assertEqual(entries[-1]["where"], "專案 / Claude")


class FakeCore:
    """Stands in for `hc`: a list of terminals, a record of what was typed."""

    def __init__(self, terminals):
        self.terminals = {t["address"]: t for t in terminals}
        self.sent = []
        self.labels = {}

    def hc(self, args, plain=False, timeout=10):
        if args[0] == "send":
            self.sent.append((args[1], args[2], plain))
            return ""
        if args[0] == "label":
            key, _, value = args[2].partition("=")
            self.terminals[args[1]]["labels"][key] = value
            return ""
        if args[0] == "read":
            return "line\nlast words\n$ "
        raise ar.HcError(f"unexpected hc {args}")

    def hc_json(self, args, timeout=10):
        if args[0] == "ls":
            return {"terminals": [{"address": a, "state": "live"} for a in self.terminals]}
        if args[0] == "get":
            return self.terminals[args[1]]
        raise ar.HcError(f"unexpected hc {args}")


class ResumePassTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.folder = os.path.join(self.tmp.name, "proj 專案")
        os.makedirs(self.folder)
        self.transcript = os.path.join(self.tmp.name, "t.jsonl")
        Path(self.transcript).write_text("{}\n", encoding="utf-8")

    def lbl(self, **kw):
        return label(cwd=self.folder, transcript=self.transcript, **kw)

    def run_pass(self, core, **kw):
        with mock.patch.object(ar, "hc", core.hc), mock.patch.object(ar, "hc_json", core.hc_json), \
                mock.patch.object(ar, "boot_id", return_value=BOOT), \
                mock.patch.object(ar.time, "sleep", lambda s: None):
            return ar.resume(**kw)

    def test_types_the_command_as_a_plain_outsider_and_records_it(self):
        core = FakeCore([terminal(lbl=self.lbl(shell=100))])
        with mock.patch.object(ar, "failed_start", return_value=None):
            out = self.run_pass(core)
        self.assertEqual([i["terminal"] for i in out["resumed"]], ["t1"])
        address, text, plain = core.sent[0]
        self.assertEqual(address, "t1")
        self.assertTrue(plain)  # no <hc from=...> wrapper around the command
        self.assertIn(f"claude '--resume' '{SID}'", text)
        self.assertIn(self.folder, text)
        self.assertEqual(ar.load_state()["sessions"][ar.session_key("claude", SID)]["last"], "typed")

    def test_dry_run_types_nothing(self):
        core = FakeCore([terminal(lbl=self.lbl(shell=100))])
        out = self.run_pass(core, dry_run=True)
        self.assertEqual(core.sent, [])
        self.assertEqual(len(out["resumed"]), 1)
        self.assertTrue(out["dry_run"])

    def test_an_agent_that_exits_right_away_is_recorded_as_failed_and_can_be_retried(self):
        core = FakeCore([terminal(lbl=self.lbl(shell=100))])
        out = self.run_pass(core)  # the fake terminal is still an idle shell with the old label: a failed start
        self.assertEqual(out["resumed"], [])
        self.assertTrue(out["skipped"][0]["reason"].startswith(ar.EXITED))
        entry = ar.load_state()["sessions"][ar.session_key("claude", SID)]
        self.assertEqual(entry["last"], "failed")
        again = self.run_pass(FakeCore([terminal(lbl=self.lbl(shell=100))]))
        self.assertEqual(len(again["skipped"]), 1)  # a manual pass tries again

    def test_the_automatic_pass_tries_each_session_once(self):
        first = FakeCore([terminal(lbl=self.lbl(shell=100, at=7))])
        self.run_pass(first, auto=True)
        self.assertEqual(len(first.sent), 1)
        second = FakeCore([terminal(lbl=self.lbl(shell=100, at=7))])
        out = self.run_pass(second, auto=True)
        self.assertEqual(second.sent, [])
        self.assertIn("already tried", out["skipped"][0]["reason"])
        third = FakeCore([terminal(lbl=self.lbl(shell=100, at=7))])
        self.run_pass(third)  # the key press still works
        self.assertEqual(len(third.sent), 1)

    def test_a_terminal_started_normally_is_left_alone(self):
        core = FakeCore([terminal("t1"), terminal("t2", pid=301)])
        out = self.run_pass(core, auto=True)
        self.assertEqual((core.sent, out["resumed"], out["skipped"]), ([], [], []))

    def test_a_second_pass_while_one_runs_does_nothing(self):
        lock = ar.runtime_lock("resume")
        try:
            out = self.run_pass(FakeCore([terminal(lbl=self.lbl(shell=100))]))
        finally:
            os.close(lock)
        self.assertTrue(out["busy"])

    def test_the_session_open_elsewhere_is_not_started_again(self):
        core = FakeCore([terminal("t1", lbl=self.lbl(shell=300), pid=300),
                         terminal("t2", pid=301, lbl=self.lbl(shell=100))])
        out = self.run_pass(core)
        self.assertEqual(core.sent, [])
        self.assertEqual([s["reason"] for s in out["skipped"]], ["open in another terminal"])


class WatchTests(Sandbox):
    """A session you quit stays quit; one that the machine took away stays for resume."""

    def watch(self, shell_alive, terminal_pid, labels):
        core = FakeCore([terminal(pid=terminal_pid, lbl=None)])
        core.terminals["t1"]["labels"] = labels
        # Each real watcher is its own process; here several share one, so the lock is faked.
        with mock.patch.object(ar, "hc", core.hc), mock.patch.object(ar, "hc_json", core.hc_json), \
                mock.patch.object(ar, "wait_exit"), mock.patch.object(ar.time, "sleep"), \
                mock.patch.object(ar, "runtime_lock", return_value=0), \
                mock.patch.object(ar, "alive", return_value=shell_alive):
            return ar.watch("t1", 200, 100), core

    def test_a_quit_agent_is_forgotten(self):
        outcome, core = self.watch(True, 100, {ar.LABEL: json.dumps(label(shell=100, pid=200))})
        self.assertTrue(outcome.startswith("forgot"))
        self.assertEqual(core.terminals["t1"]["labels"][ar.LABEL], "")

    def test_a_shutdown_keeps_the_session(self):
        outcome, core = self.watch(False, 100, {ar.LABEL: json.dumps(label(shell=100, pid=200))})
        self.assertEqual(outcome, "terminal went with it")
        self.assertNotEqual(core.terminals["t1"]["labels"][ar.LABEL], "")

    def test_a_replaced_terminal_or_newer_session_is_not_touched(self):
        outcome, _ = self.watch(True, 555, {ar.LABEL: json.dumps(label(shell=100, pid=200))})
        self.assertEqual(outcome, "terminal was replaced")
        outcome, core = self.watch(True, 100, {ar.LABEL: json.dumps(label(shell=100, pid=999))})
        self.assertEqual(outcome, "a newer session took over")


class HookTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.core = FakeCore([terminal(pid=500, lbl=None)])
        self.env = {"HC_TERMINAL_ID": "t1"}

    def hook(self, agent, payload, env=None, agent_pid=600, where="direct"):
        patches = [mock.patch.object(ar, "hc", self.core.hc), mock.patch.object(ar, "hc_json", self.core.hc_json),
                   mock.patch.object(ar, "find_agent", return_value=agent_pid),
                   mock.patch.object(ar, "placement", return_value=where),
                   mock.patch.object(ar, "proc_argv", return_value=["claude.exe", "--model", "opus",
                                                                    "--dangerously-skip-permissions"]),
                   mock.patch.object(ar.subprocess, "Popen")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return ar.hook(agent, json.dumps(payload, ensure_ascii=False), {**self.env, **(env or {})})

    def payload(self, **kw):
        return {"session_id": SID, "cwd": "C:\\Users\\王小明\\專案 一", "transcript_path": "C:\\Users\\王小明\\.claude\\p\\x.jsonl",
                "hook_event_name": "SessionStart", **kw}

    def saved(self):
        return json.loads(self.core.terminals["t1"]["labels"][ar.LABEL])

    def test_notes_the_session_with_folder_agent_flags_and_shell_identity(self):
        self.assertEqual(self.hook("claude", self.payload()), "saved")
        saved = self.saved()
        self.assertEqual((saved["agent"], saved["session"], saved["pid"], saved["shell"]), ("claude", SID, 600, 500))
        self.assertEqual(saved["cwd"], "C:\\Users\\王小明\\專案 一")
        self.assertEqual(saved["boot"], ar.boot_id())
        self.assertEqual(saved["args"], ["--model", "opus"])  # the bypass flag was not saved
        self.assertIn("at", saved)

    def test_codex_is_noted_the_same_way(self):
        self.assertEqual(self.hook("codex", self.payload(session_id=SID2), env={"CODEX_THREAD_ID": SID2}), "saved")
        self.assertEqual(self.saved()["agent"], "codex")

    def test_the_same_session_is_not_noted_twice(self):
        self.hook("claude", self.payload())
        self.assertEqual(self.hook("claude", self.payload()), "already noted")

    def test_ignored_cases(self):
        self.assertEqual(ar.hook("claude", "{}", {}), "not in a Herdr terminal")
        self.assertEqual(self.hook("claude", self.payload(), where="nested"), "agent runs nested")
        self.assertEqual(self.hook("claude", self.payload(), where="outside"), "agent runs outside")
        self.assertEqual(self.hook("claude", self.payload(agent_id="sub1")), "subagent")
        self.assertEqual(self.hook("claude", self.payload(session_id="x'; calc")), "no usable session id")
        self.assertEqual(self.hook("claude", self.payload(hook_event_name="PreToolUse")), "not SessionStart")
        self.assertEqual(self.hook("codex", self.payload(session_id=SID), env={"CODEX_THREAD_ID": SID2}), "codex sub-thread")
        self.assertEqual(self.hook("claude", {"session_id": SID, "cwd": "C:\\x"} and {**self.payload(), "transcript_path": ""}),
                         "no transcript: the session can't be resumed")

    def test_input_that_is_not_json_is_ignored(self):
        with mock.patch.object(ar, "hc", self.core.hc), mock.patch.object(ar, "hc_json", self.core.hc_json):
            self.assertEqual(ar.hook("claude", "not json", self.env), "input is not JSON")
            self.assertEqual(ar.hook("claude", "[1]", self.env), "input is not an object")

    def test_a_terminal_that_is_gone_is_not_labelled(self):
        self.core.terminals["t1"]["state"] = "finished"
        self.assertEqual(self.hook("claude", self.payload()), "terminal not live")
        self.assertNotIn(ar.LABEL, self.core.terminals["t1"]["labels"])


class CliTests(Sandbox):
    """The command line the way the hook, the mod and the user call it."""

    def run_cli(self, *args, stdin=""):
        # No `hc` is findable (empty PATH, LOCALAPPDATA in the sandbox): these runs can never
        # reach a real core, and never start one either.
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PATH": os.environ.get("SystemRoot", "C:\\Windows"),
               "LOCALAPPDATA": os.path.join(self.tmp.name, "local")}
        for name in ("HC_TERMINAL_ID", "HC_TERMINAL_SOCKET", "HC_SCOPE", "HC_SESSION"):
            env.pop(name, None)
        return subprocess.run([sys.executable, "-I", str(Path(ar.__file__)), *args], input=stdin.encode("utf-8"),
                              capture_output=True, env=env, timeout=60)

    def test_hook_outside_herdr_is_silent_and_exits_zero(self):
        result = self.run_cli("hook", "claude", stdin=json.dumps({"session_id": SID, "cwd": "C:\\專案"}))
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b"", b""))

    def test_auto_toggle(self):
        self.assertEqual(json.loads(self.run_cli("auto", "status").stdout)["auto"], False)
        self.assertEqual(json.loads(self.run_cli("auto", "on").stdout)["auto"], True)
        self.assertEqual(json.loads(self.run_cli("auto", "status").stdout)["auto"], True)
        self.assertEqual(json.loads(self.run_cli("auto", "off").stdout)["auto"], False)
        self.assertEqual(self.run_cli("auto", "bogus").returncode, 2)

    def test_status_without_a_core_reports_an_error_not_a_crash(self):
        env_missing = self.run_cli("status")
        out = json.loads(env_missing.stdout.decode("utf-8"))
        self.assertIn("error", out)
        self.assertEqual(env_missing.returncode, 1)

    def test_doctor_reports(self):
        report = json.loads(self.run_cli("doctor").stdout.decode("utf-8"))
        self.assertEqual(report["platform"], "win32")
        self.assertIn("core", report)

    def test_unknown_command_prints_usage(self):
        result = self.run_cli("bogus")
        self.assertEqual(result.returncode, 2)
        self.assertIn(b"hook <agent>", result.stderr)


if __name__ == "__main__":
    unittest.main()
