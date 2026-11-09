"""Installer tests on Windows: a throw-away home and app home, an in-memory registry.
Nothing here touches the real Herdr app, ~/.claude, ~/.codex or HKCU."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

if sys.platform != "win32":
    raise unittest.SkipTest("Windows-only tests")

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("install", ROOT / "scripts/install.py")
inst = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inst)

KEYS = {"global": {"ctrl-shift-p": "app.palette"}, "terminal": {"ctrl-shift-v": "terminal.paste"}}
CLAUDE = {"model": "opus", "note": "設定 with 中文",
          "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo mine"}]}],
                    "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "echo prompt"}]}]}}
CODEX = {"hooks": {"SessionStart": [{"matcher": "startup|resume", "hooks": [{"type": "command", "command": "echo codex"}]}]}}


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name).resolve()  # the installer resolves 8.3 short names too
        self.home, self.app = root / "home 家", root / "app"
        (self.home / ".claude").mkdir(parents=True)
        (self.home / ".codex").mkdir()
        self.app.mkdir()
        self.claude = self.home / ".claude" / "settings.json"
        self.codex = self.home / ".codex" / "hooks.json"
        self.keys = self.app / "keys.json"
        self.claude.write_text(json.dumps(CLAUDE, ensure_ascii=False, indent=2), encoding="utf-8")
        self.codex.write_text(json.dumps(CODEX, indent=2), encoding="utf-8")
        self.keys.write_text(json.dumps(KEYS, indent=2), encoding="utf-8")
        self.registry = {"value": None}
        for name, side in (("registry_get", lambda: self.registry["value"]),
                           ("registry_set", lambda v: self.registry.update(value=v)),
                           ("registry_delete", lambda: self.registry.update(value=None))):
            patcher = mock.patch.object(inst, name, side)
            patcher.start()
            self.addCleanup(patcher.stop)

    def install(self, *flags, expect_error=False):
        argv = ["--app-home", str(self.app), "--home", str(self.home), "--no-adopt", *flags]
        out = io.StringIO()

        # `herdr-app check` is faked here (the installer's only subprocess with --no-adopt);
        # RealCheckTests runs it for real.
        def fake_run(cmd, **kw):
            self.assertEqual(cmd[0], "herdr-app")
            return subprocess.CompletedProcess(cmd, 0)

        with mock.patch.object(inst.subprocess, "run", side_effect=fake_run), contextlib.redirect_stdout(out):
            if expect_error:
                with self.assertRaises((ValueError, OSError)):
                    inst.main(argv)
            else:
                inst.main(argv)
        return out.getvalue()

    def read(self, path):
        return json.loads(Path(path).read_text(encoding="utf-8"))

    def snapshot(self):
        files = {}
        for base in (self.home, self.app):
            for path in base.rglob("*"):
                if path.is_file() and "backups" not in path.parts:
                    files[str(path)] = path.read_bytes()
        return files

    def hook_commands(self, config):
        return [h["command"] for g in config["hooks"]["SessionStart"] for h in g["hooks"]]

    # ------------------------------------------------------------ dry run

    def test_dry_run_changes_nothing_and_says_what_it_would_do(self):
        before = self.snapshot()
        out = self.install("--dry-run", "--auto")
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.app / "backups").exists())
        self.assertIsNone(self.registry["value"])
        self.assertIn("Dry run: no files changed.", out)
        self.assertIn("Hook:", out)
        self.assertIn("Background watcher: registered", out)

    # ------------------------------------------------------------ install

    def test_install_adds_hooks_after_the_existing_ones_and_keeps_everything_else(self):
        self.install()
        claude, codex = self.read(self.claude), self.read(self.codex)
        self.assertEqual(claude["hooks"]["SessionStart"][0], CLAUDE["hooks"]["SessionStart"][0])  # position kept
        self.assertEqual(claude["hooks"]["UserPromptSubmit"], CLAUDE["hooks"]["UserPromptSubmit"])
        self.assertEqual((claude["model"], claude["note"]), ("opus", "設定 with 中文"))
        self.assertEqual(codex["hooks"]["SessionStart"][0], CODEX["hooks"]["SessionStart"][0])
        self.assertEqual(len(claude["hooks"]["SessionStart"]), 2)
        self.assertEqual(len(codex["hooks"]["SessionStart"]), 2)
        mine = self.hook_commands(claude)[-1]
        script = (self.app / "mods" / "agent-resume" / "agent_resume.py").as_posix()
        self.assertEqual(mine, f'python "{script}" hook claude')
        self.assertTrue(self.hook_commands(codex)[-1].endswith("hook codex"))

    def test_install_does_not_override_keys(self):
        self.install()
        keys = self.read(self.keys)
        self.assertEqual(keys["global"]["ctrl-shift-r"], "mod.open:agent-resume")
        self.assertEqual(keys["global"]["ctrl-shift-p"], "app.palette")
        self.assertEqual(keys["terminal"], KEYS["terminal"])

    def test_a_key_conflict_refuses_and_changes_nothing(self):
        self.keys.write_text(json.dumps({"global": {"ctrl-shift-r": "something.else"}}), encoding="utf-8")
        before = self.snapshot()
        self.install(expect_error=True)
        self.assertEqual(self.snapshot(), before)

    def test_mod_files_are_installed_with_the_windows_interpreter_name(self):
        self.install()
        mod = self.app / "mods" / "agent-resume"
        self.assertEqual({p.name for p in mod.iterdir()},
                         {"main.js", "summary.js", "mod.json", "agent_resume.py", "winproc.py", inst.MARKER})
        main = (mod / "main.js").read_text(encoding="utf-8")
        self.assertIn('const PYTHON = "python";', main)
        self.assertNotIn('"python3"', main)

    def test_only_claude_and_codex_get_hooks_on_windows(self):
        for folder in (".grok", ".gemini", ".kiro", ".pi"):
            (self.home / folder).mkdir()
        self.install()
        self.assertEqual({p.name for p in self.home.iterdir()}, {".claude", ".codex", ".grok", ".gemini", ".kiro", ".pi"})
        for folder in (".grok", ".gemini", ".kiro", ".pi"):
            self.assertEqual(list((self.home / folder).rglob("*")), [], folder)

    def test_an_agent_that_is_not_set_up_gets_nothing(self):
        import shutil
        shutil.rmtree(self.home / ".codex")
        self.install()
        self.assertFalse((self.home / ".codex").exists())

    def test_install_is_idempotent_and_update_requires_the_flag(self):
        self.install()
        self.install(expect_error=True)  # already installed: needs --update
        self.install("--update")
        self.assertEqual(len(self.read(self.claude)["hooks"]["SessionStart"]), 2)
        self.assertEqual(len([c for c in self.hook_commands(self.read(self.claude)) if "agent_resume.py" in c]), 1)

    def test_a_hand_edited_install_is_not_overwritten_by_update(self):
        self.install()
        (self.app / "mods" / "agent-resume" / "main.js").write_text("// mine\n", encoding="utf-8")
        self.install("--update", expect_error=True)
        self.assertEqual((self.app / "mods" / "agent-resume" / "main.js").read_text(encoding="utf-8"), "// mine\n")

    def test_crlf_files_keep_their_line_endings(self):
        text = json.dumps(CLAUDE, ensure_ascii=False, indent=2).replace("\n", "\r\n")
        self.claude.write_bytes(text.encode("utf-8"))
        self.install()
        raw = self.claude.read_bytes()
        self.assertIn(b"\r\n", raw)
        self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))
        self.assertIn("設定 with 中文".encode("utf-8"), raw)  # UTF-8, not the console code page

    def test_a_broken_settings_file_stops_the_install_before_any_change(self):
        self.claude.write_text("{ not json", encoding="utf-8")
        before = self.snapshot()
        self.install(expect_error=True)
        self.assertEqual(self.snapshot(), before)

    # ------------------------------------------------------------ backup and restore

    def test_every_change_writes_a_backup_with_a_manifest(self):
        self.install()
        (backup,) = inst.list_backups(self.app)
        manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
        originals = {item["original"] for item in manifest["files"]}
        self.assertEqual(originals, {str(self.keys), str(self.claude), str(self.codex)})
        self.assertEqual(json.loads((backup / "claude-settings.json").read_text(encoding="utf-8")), CLAUDE)

    def test_restore_returns_every_file_to_what_it_was(self):
        before = self.snapshot()
        self.install("--auto")
        self.assertNotEqual(self.snapshot(), before)
        out = self.install("--restore")
        self.assertEqual(self.snapshot(), before)
        self.assertIn("Restored backup", out)
        self.assertIsNone(self.registry["value"])

    def test_restore_dry_run_changes_nothing(self):
        self.install()
        after = self.snapshot()
        out = self.install("--restore", "--dry-run")
        self.assertEqual(self.snapshot(), after)
        self.assertIn("restore", out)

    def test_restore_a_named_backup_and_list_them(self):
        self.install()
        self.install("--update")
        names = [p.name for p in inst.list_backups(self.app)]
        self.assertEqual(len(names), 2)
        self.assertEqual(self.install("--list-backups").split(), names)
        self.install("--restore", names[0])
        self.assertFalse((self.app / "mods" / "agent-resume").exists())  # the very first state: no mod
        self.install("--restore", "nonexistent", expect_error=True)

    def test_restore_with_no_backup_says_so(self):
        self.install("--restore", expect_error=True)

    # ------------------------------------------------------------ uninstall

    def test_uninstall_removes_only_our_pieces(self):
        before = self.snapshot()
        self.install("--auto")
        self.install("--uninstall")
        after = self.snapshot()
        self.assertEqual(self.read(self.claude), CLAUDE)
        self.assertEqual(self.read(self.codex), CODEX)
        self.assertEqual(self.read(self.keys), KEYS)
        self.assertFalse((self.app / "mods" / "agent-resume").exists())
        self.assertIsNone(self.registry["value"])
        self.assertEqual({k for k in after}, {k for k in before})

    def test_uninstall_leaves_a_user_hook_that_merely_mentions_python(self):
        config = json.loads(json.dumps(CLAUDE))
        config["hooks"]["SessionStart"].append({"hooks": [{"type": "command", "command": "python my_script.py hook x"}]})
        self.claude.write_text(json.dumps(config), encoding="utf-8")
        self.install()
        self.install("--uninstall")
        self.assertEqual(self.read(self.claude)["hooks"]["SessionStart"], config["hooks"]["SessionStart"])

    # ------------------------------------------------------------ background watcher

    def test_the_watcher_is_only_registered_with_auto(self):
        self.install()
        self.assertIsNone(self.registry["value"])
        self.install("--update", "--auto")
        value = self.registry["value"]
        self.assertTrue(value.endswith('agent_resume.py" auto-watch'), value)
        self.assertIn("pythonw", value.lower())

    def test_no_auto_takes_the_watcher_out_and_a_plain_update_keeps_it(self):
        self.install("--auto")
        self.install("--update")
        self.assertIsNotNone(self.registry["value"])
        self.install("--update", "--no-auto")
        self.assertIsNone(self.registry["value"])

    def test_auto_and_no_auto_together_are_refused(self):
        self.install("--auto", "--no-auto", expect_error=True)

    def test_a_powershell_profile_is_never_touched(self):
        profile = self.home / "Documents" / "PowerShell" / "Microsoft.PowerShell_profile.ps1"
        profile.parent.mkdir(parents=True)
        profile.write_text("# mine\n", encoding="utf-8")
        before = self.snapshot()
        self.install("--auto")
        self.assertEqual(profile.read_text(encoding="utf-8"), "# mine\n")
        self.assertEqual(inst.shell_files(self.home), [])
        self.assertIn(str(profile), before)

    def test_a_failed_write_rolls_everything_back(self):
        before = self.snapshot()
        with mock.patch.object(inst, "write_hook", side_effect=OSError("disk full")):
            self.install("--auto", expect_error=True)
        self.assertEqual(self.snapshot(), before)
        self.assertIsNone(self.registry["value"])


class RealCheckTests(unittest.TestCase):
    """`herdr-app check` only loads and renders the mod once; it changes nothing."""

    @unittest.skipUnless(inst.shutil.which("herdr-app"), "herdr-app is not installed")
    def test_the_mod_passes_herdr_app_check(self):
        with tempfile.TemporaryDirectory() as app:
            result = subprocess.run(["herdr-app", "check", str(ROOT / "mods" / "agent-resume")],
                                    env={**__import__("os").environ, "HERDR_APP_HOME": app},
                                    capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
