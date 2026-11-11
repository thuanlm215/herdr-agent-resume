import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from contextlib import redirect_stdout
from io import StringIO

spec = importlib.util.spec_from_file_location("installer", Path(__file__).parents[1] / "scripts/install.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)

KEY = {"global": {"ctrl-shift-r": "mod.open:agent-resume"}}
CMD = installer.hook_command("/app/mods/agent-resume/agent_resume.py", "codex")


class KeyTests(unittest.TestCase):
    def test_conflict_leaves_input_unchanged(self):
        original = {"global": {"ctrl-shift-r": "something.else"}}
        with self.assertRaisesRegex(ValueError, "Shortcut conflict"):
            installer.merge_keys(original, KEY)
        self.assertEqual(original["global"]["ctrl-shift-r"], "something.else")

    def test_install_and_uninstall_keep_other_keys(self):
        existing = {"global": {"ctrl-shift-f": "layout.toggle:panel"}}
        merged = installer.merge_keys(existing, KEY)
        self.assertEqual(merged["global"], {"ctrl-shift-f": "layout.toggle:panel", "ctrl-shift-r": "mod.open:agent-resume"})
        self.assertEqual(installer.merge_keys(merged, KEY), merged)
        self.assertEqual(installer.remove_keys(merged, KEY), existing)

    def test_uninstall_leaves_a_key_someone_rebound(self):
        rebound = {"global": {"ctrl-shift-r": "something.else"}}
        self.assertEqual(installer.remove_keys(rebound, KEY), rebound)


class HookTests(unittest.TestCase):
    def test_appends_after_existing_groups(self):
        existing = {"model": "x", "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "herdr"}]}],
                                            "Stop": [{"hooks": [{"type": "command", "command": "orca"}]}]}}
        merged = installer.add_hook(existing, CMD)
        self.assertEqual(merged["hooks"]["SessionStart"][0], existing["hooks"]["SessionStart"][0])
        self.assertEqual(merged["hooks"]["SessionStart"][1], {"hooks": [{"type": "command", "command": CMD, "timeout": 10}]})
        self.assertEqual(merged["hooks"]["Stop"], existing["hooks"]["Stop"])
        self.assertEqual(merged["model"], "x")
        self.assertEqual(len(existing["hooks"]["SessionStart"]), 1)

    def test_repeat_install_does_not_add_a_second_hook(self):
        once = installer.add_hook({}, CMD)
        self.assertEqual(installer.add_hook(once, CMD), once)
        moved = installer.add_hook(once, installer.hook_command("/elsewhere/agent_resume.py", "codex"))
        self.assertEqual(len(moved["hooks"]["SessionStart"]), 1)
        self.assertIn("/elsewhere/", moved["hooks"]["SessionStart"][0]["hooks"][0]["command"])

    def test_uninstall_removes_only_ours(self):
        existing = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "herdr"}]}]}}
        self.assertEqual(installer.remove_hook(installer.add_hook(existing, CMD)), existing)
        self.assertEqual(installer.remove_hook(installer.add_hook({}, CMD)), {"hooks": {}})


class OtherAgentHookTests(unittest.TestCase):
    def test_agy_block_is_added_beside_other_hooks_and_removed(self):
        cmd = installer.hook_command("/app/mods/agent-resume/agent_resume.py", "agy")
        existing = {"herdr": {"PreInvocation": [{"command": "herdr", "type": "command"}]}}
        merged = installer.add_agy_hook(existing, cmd)
        self.assertEqual(merged["herdr"], existing["herdr"])
        self.assertIn("hook agy", merged["agent-resume"]["SessionStart"][0]["command"])
        self.assertEqual(len(merged["agent-resume"]["PreInvocation"]), 1)
        self.assertEqual(installer.add_agy_hook(merged, cmd), merged)
        moved = installer.add_agy_hook(merged, installer.hook_command("/elsewhere/agent_resume.py", "agy"))
        self.assertIn("/elsewhere/", moved["agent-resume"]["SessionStart"][0]["command"])
        self.assertEqual(installer.remove_agy_hook(moved), existing)
        with self.assertRaisesRegex(ValueError, "isn't this mod's"):
            installer.add_agy_hook({"agent-resume": {"SessionStart": [{"command": "mine"}]}}, cmd)

    def test_kiro_entries_keep_the_other_hooks(self):
        cmd = installer.hook_command("/app/agent_resume.py", "kiro")
        existing = {"version": "v1", "hooks": [{"name": "other", "trigger": "Stop", "action": {"type": "command", "command": "x"}}]}
        merged = installer.add_kiro_hook(existing, cmd)
        self.assertEqual(merged["hooks"][0]["name"], "other")
        self.assertEqual([hook["trigger"] for hook in merged["hooks"][1:]], ["SessionStart", "UserPromptSubmit"])
        again = installer.add_kiro_hook(merged, installer.hook_command("/moved/agent_resume.py", "kiro"))
        self.assertEqual(len(again["hooks"]), 3)
        self.assertIn("/moved/", again["hooks"][1]["action"]["command"])
        self.assertEqual(installer.remove_kiro_hook(again)["hooks"], existing["hooks"])

    def test_install_update_uninstall_never_change_kiro_profiles_or_settings(self):
        for default in (None, "kiro", "work"):
            for preexisting in (False, True):
                with self.subTest(default=default, preexisting=preexisting), tempfile.TemporaryDirectory() as directory:
                    home = Path(directory)
                    app = home / "app"
                    app.mkdir()
                    (app / "keys.json").write_text('{}')
                    settings = home / ".kiro/settings/cli.json"
                    settings.parent.mkdir(parents=True)
                    original = json.dumps({"chat.defaultAgent": default, "other": 42})
                    settings.write_text(original)
                    agent = home / ".kiro/agents/kiro.json"
                    if preexisting:
                        agent.parent.mkdir()
                        agent.write_text('{"name":"kiro","prompt":"mine"}')
                    agent_before = agent.read_bytes() if agent.exists() else None
                    base = ["install.py", "--home", str(home), "--app-home", str(app), "--no-adopt"]
                    with mock.patch.object(installer.subprocess, "run") as run:
                        for flag in ([], ["--update"], ["--uninstall"]):
                            with mock.patch("sys.argv", base + flag), redirect_stdout(StringIO()):
                                installer.main()
                            self.assertEqual(settings.read_text(), original)
                            self.assertEqual(agent.read_bytes() if agent.exists() else None, agent_before)
                        self.assertTrue(all(call.args[0][0] == "herdr-app" for call in run.call_args_list))
                    self.assertFalse((home / ".kiro/hooks/agent-resume.json").exists())

    def test_only_agents_set_up_on_this_machine(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / ".grok").mkdir()
            (home / ".gemini" / "config").mkdir(parents=True)
            (home / ".kiro").mkdir()
            found = {name: kind for name, _, _, kind in installer.agent_configs(home)}
            self.assertEqual(found, {"grok": "group", "agy": "agy", "kiro": "kiro"})


class ShellTests(unittest.TestCase):
    SCRIPT = "/app/mods/agent-resume/agent_resume.py"

    def test_block_is_added_once_and_removed_cleanly(self):
        block = installer.snippet("bash", self.SCRIPT)
        for original in ("", "alias ll='ls -l'\n", "export A=1"):
            added = installer.with_block(original, block)
            self.assertEqual(added.count(installer.BEGIN), 1)
            self.assertEqual(installer.with_block(added, block), added)
            self.assertEqual(installer.without_block(added), original if original.endswith("\n") or not original else original + "\n")

    def test_an_older_block_is_replaced_in_place(self):
        text = "a\n\n" + installer.snippet("zsh", "/old/agent_resume.py") + "b\n"
        new = installer.with_block(text, installer.snippet("zsh", self.SCRIPT))
        self.assertTrue(new.startswith("a\n\n" + installer.BEGIN))
        self.assertTrue(new.endswith(installer.END + "\nb\n"))
        self.assertNotIn("/old/", new)

    def test_a_broken_block_changes_nothing(self):
        with self.assertRaisesRegex(ValueError, "not their end"):
            installer.without_block("x\n" + installer.BEGIN + "\nfoo\n")

    def test_refuses_a_path_with_a_quote(self):
        with self.assertRaises(ValueError):
            installer.snippet("fish", "/it's/agent_resume.py")

    def test_changes_per_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / ".config" / "fish").mkdir(parents=True)
            (home / ".bashrc").write_text("export A=1\n")
            self.assertEqual(installer.shell_changes(home, self.SCRIPT, "refresh"), [])
            self.assertEqual(installer.shell_changes(home, self.SCRIPT, "remove"), [])
            added = installer.shell_changes(home, self.SCRIPT, "add")
            self.assertEqual([p.name for p, _ in added], ["agent-resume.fish", ".bashrc"])
            installer.apply_text_changes(added)
            self.assertIn("fish_prompt", (home / ".config/fish/conf.d/agent-resume.fish").read_text())
            self.assertEqual(installer.shell_changes(home, self.SCRIPT, "add"), [])
            self.assertEqual(len(installer.shell_changes(home, "/moved/agent_resume.py", "refresh")), 2)
            installer.apply_text_changes(installer.shell_changes(home, self.SCRIPT, "remove"))
            self.assertFalse((home / ".config/fish/conf.d/agent-resume.fish").exists())
            self.assertEqual((home / ".bashrc").read_text(), "export A=1\n")


class ExtensionTests(unittest.TestCase):
    def test_install_update_uninstall_preserves_user_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            app = home / "app"
            app.mkdir()
            (app / "keys.json").write_text('{"global":{"ctrl-x":"mine"}}')
            (home / ".zshrc").write_text("export KEEP_ME=yes\n")
            claude = home / ".claude/settings.json"
            claude.parent.mkdir()
            original_claude = {"model": "mine", "hooks": {"Stop": [{"hooks": [{"command": "mine"}]}]}}
            claude.write_text(json.dumps(original_claude))
            profile = home / ".omp/profiles/work/agent"
            profile.mkdir(parents=True)
            base = ["install.py", "--home", str(home), "--app-home", str(app), "--no-adopt"]
            with mock.patch.object(installer.subprocess, "run"):
                for flags in (["--auto", "--agents", "omp", "claude", "codex"], ["--update"]):
                    with mock.patch("sys.argv", base + flags), redirect_stdout(StringIO()):
                        installer.main()
                self.assertEqual((home / ".zshrc").read_text().count(installer.BEGIN), 1)
                self.assertTrue((profile / "extensions/herdr-agent-resume.js").is_file())
                with mock.patch("sys.argv", base + ["--uninstall"]), redirect_stdout(StringIO()):
                    installer.main()
            self.assertEqual((home / ".zshrc").read_text(), "export KEEP_ME=yes\n")
            self.assertEqual(json.loads(claude.read_text()), original_claude)
            self.assertFalse((profile / "extensions/herdr-agent-resume.js").exists())
            self.assertFalse((home / ".omp/agent/extensions/herdr-agent-resume.js").exists())
            self.assertEqual(json.loads((app / "keys.json").read_text()), {"global": {"ctrl-x": "mine"}})

    def test_unrelated_extension_is_never_overwritten_or_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            path = home / ".omp/agent/extensions/herdr-agent-resume.js"
            path.parent.mkdir(parents=True)
            path.write_text("user-owned extension")
            with self.assertRaises(ValueError):
                installer.extension_changes(home, "/app/agent_resume.py", False)
            installer.apply_text_changes(installer.extension_changes(home, "/app/agent_resume.py", True))
            self.assertEqual(path.read_text(), "user-owned extension")


class UpdateTests(unittest.TestCase):
    def test_update_refuses_edited_or_extra_files(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory)
            (destination / "main.js").write_text("original")
            marker = {"package": installer.PACKAGE, "files": {"main.js": installer.digest(destination / "main.js")}}
            (destination / "jsconfig.json").write_text("generated by Herdr")
            (destination / "__pycache__").mkdir()
            installer.verify_installed(destination, marker)
            (destination / "main.js").write_text("edited")
            with self.assertRaisesRegex(ValueError, "was edited"):
                installer.verify_installed(destination, marker)
            (destination / "main.js").write_text("original")
            (destination / "notes.md").write_text("mine")
            with self.assertRaisesRegex(ValueError, "Extra installed file"):
                installer.verify_installed(destination, marker)


if __name__ == "__main__":
    unittest.main()
