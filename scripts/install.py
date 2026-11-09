#!/usr/bin/env python3
"""Install Agent resume: the mod, its shortcut and the SessionStart hooks.

- copies mods/agent-resume into the Herdr app's mods folder (after `herdr-app check`);
- adds Ctrl+Shift+R (`mod.open:agent-resume`) to keys.json, refusing a key already in use;
- appends a SessionStart hook next to the hooks already there, backing each file up first.
  Windows: Claude Code (~/.claude/settings.json) and Codex (~/.codex/hooks.json).
  Linux/macOS also: Grok, Antigravity, Kiro V3 and Pi;
- notes Claude Code sessions that are already running (`agent_resume.py adopt`);
- `--auto`: Linux/macOS add lines to the shell's startup file. Windows registers the
  background watcher under HKCU ...\\Run instead, so no PowerShell profile is edited; the
  watcher does nothing until `agent_resume.py auto on`.

Every run that changes files first writes a backup and a manifest to
<app home>/backups/agent-resume/<stamp>/. `--list-backups` shows them, `--restore [STAMP]`
puts one back, `--dry-run` shows what any action would change without touching anything.
No layout change is needed. `--uninstall` takes all of it out again.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

IS_WIN = sys.platform == "win32"
ROOT = Path(__file__).resolve().parents[1]
MOD_NAME = "agent-resume"
PACKAGE = "herdr-agent-resume"
VERSION = "0.4.1+windows.1"
MARKER = ".agent-resume-install.json"
FILES = ("main.js", "summary.js", "mod.json", "agent_resume.py", "winproc.py")
GENERATED = {"gpui-kit.d.ts", "jsconfig.json", "__pycache__"}
HOOK_RE = re.compile(r"agent_resume\.py[\"']? hook ")
BEGIN, END = "# >>> agent-resume >>>", "# <<< agent-resume <<<"
ABOUT = ("# Agent resume (herdr-agent-resume): a restored Herdr terminal starts its agent again.\n"
         "# Set AGENT_RESUME_AUTO=0 to pause it; `install.py --no-auto` removes these lines.\n")
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "HerdrAgentResume"
WINDOWS_AGENT_NAMES = ("claude", "codex")
SNIPPETS = {
    # After config.fish has run (PATH is set), at the first prompt.
    "fish": """if status is-interactive; and set -q HC_TERMINAL_ID
    function __agent_resume --on-event fish_prompt
        functions -e __agent_resume
        if test "$AGENT_RESUME_AUTO" != 0; and test -f '{script}'
            command python3 '{script}' here; or command python3 '{script}' after $status
        end
    end
end
""",
    "bash": """if [[ $- == *i* && -n "$HC_TERMINAL_ID" ]]; then
    __agent_resume() {
        PROMPT_COMMAND="${PROMPT_COMMAND/__agent_resume;/}"
        unset -f __agent_resume
        if [[ "$AGENT_RESUME_AUTO" != 0 && -f '{script}' ]]; then
            command python3 '{script}' here || command python3 '{script}' after $?
        fi
    }
    PROMPT_COMMAND="__agent_resume;${PROMPT_COMMAND}"
fi
""",
    "zsh": """if [[ -o interactive && -n "$HC_TERMINAL_ID" ]]; then
    __agent_resume() {
        add-zsh-hook -d precmd __agent_resume
        unfunction __agent_resume
        if [[ "$AGENT_RESUME_AUTO" != 0 && -f '{script}' ]]; then
            command python3 '{script}' here || command python3 '{script}' after $?
        fi
    }
    autoload -Uz add-zsh-hook && add-zsh-hook precmd __agent_resume
fi
""",
}


# ---------------------------------------------------------------- files

def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_text(path):
    # UTF-8 (never the console code page), with universal newlines like text mode.
    return path.read_bytes().decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")


def read_json(path):
    return json.loads(read_text(path)) if path.exists() else {}


def newline_of(path):
    """The line ending a file already uses, so an edit doesn't rewrite every line."""
    if path.exists() and b"\r\n" in path.read_bytes()[:65536]:
        return "\r\n"
    return "\n"


def write_text(path, text, newline="\n"):
    """Write UTF-8 (never the console code page) atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline=newline, dir=path.parent,
                                     prefix=".agent-resume-", delete=False) as stream:
        stream.write(text)
        temp = Path(stream.name)
    if path.exists():
        os.chmod(temp, path.stat().st_mode & 0o777)
    elif not IS_WIN:
        os.chmod(temp, 0o644)
    temp.replace(path)


def atomic_json(path, value):
    write_text(path, json.dumps(value, indent=2, ensure_ascii=False) + "\n", newline_of(path))


def merge_keys(existing, snippet):
    merged = json.loads(json.dumps(existing))
    for section, bindings in snippet.items():
        target = merged.setdefault(section, {})
        for key, action in bindings.items():
            if key in target and target[key] != action:
                raise ValueError(f"Shortcut conflict in {section}: {key} is {target[key]!r}. No files changed.")
            target[key] = action
    return merged


def remove_keys(existing, snippet):
    merged = json.loads(json.dumps(existing))
    for section, bindings in snippet.items():
        target = merged.get(section, {})
        for key, action in bindings.items():
            if target.get(key) == action:
                del target[key]
    return merged


# ---------------------------------------------------------------- hooks

def forward(path):
    """A path spelled with forward slashes: bash, cmd and PowerShell all accept it, and a
    backslash would be eaten by bash."""
    return str(path).replace("\\", "/")


def find_python():
    """The interpreter the hooks run. `python` on Windows (there is no python3)."""
    name = "python" if IS_WIN else "python3"
    found = shutil.which(name)
    if not found:
        raise ValueError(f"`{name}` is not on PATH, so the agents' hooks couldn't run it. No files changed.")
    if IS_WIN and "WindowsApps" in found and Path(found).stat().st_size == 0:
        raise ValueError("`python` is the Microsoft Store stub, not a real Python. Install Python 3.8+. No files changed.")
    return name


def hook_command(script, agent):
    if IS_WIN:
        return f'{find_python()} "{forward(script)}" hook {agent}'
    return f"python3 '{script}' hook {agent}"


def is_ours(group):
    return any(HOOK_RE.search(str(h.get("command", ""))) for h in group.get("hooks", []) if isinstance(h, dict))


def add_hook(config, command):
    """`config` with one SessionStart group running `command` at the end; existing groups
    keep their places (Codex remembers which hooks you trusted by position)."""
    merged = json.loads(json.dumps(config))
    hooks = merged.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("`hooks` is not an object")
    groups = hooks.setdefault("SessionStart", [])
    ours = [g for g in groups if isinstance(g, dict) and is_ours(g)]
    if ours:
        for group in ours:
            for h in group.get("hooks", []):
                if HOOK_RE.search(str(h.get("command", ""))):
                    h["command"] = command
        return merged
    groups.append({"hooks": [{"type": "command", "command": command, "timeout": 10}]})
    return merged


def remove_hook(config):
    merged = json.loads(json.dumps(config))
    groups = merged.get("hooks", {}).get("SessionStart")
    if isinstance(groups, list):
        merged["hooks"]["SessionStart"] = [g for g in groups if not (isinstance(g, dict) and is_ours(g))]
        if not merged["hooks"]["SessionStart"]:
            del merged["hooks"]["SessionStart"]
    return merged


def snippet(shell, script):
    if "'" in str(script):
        raise ValueError(f"Can't write a startup line for a path with a quote: {script}")
    body = SNIPPETS[shell].replace("{script}", str(script))
    return f"{BEGIN}\n{ABOUT}{body}{END}\n"


def without_block(text):
    """`text` without our block (and the blank line we put before it)."""
    start = text.find(BEGIN)
    if start < 0:
        return text
    end = text.find(END, start)
    if end < 0:
        raise ValueError("Found the start of agent-resume's startup lines but not their end. No files changed.")
    end += len(END)
    if text[end:end + 1] == "\n":
        end += 1
    head = text[:start]
    if head.endswith("\n\n"):
        head = head[:-1]
    return head + text[end:]


def with_block(text, block):
    """`text` with our block at the end (replacing an older one in place)."""
    start = text.find(BEGIN)
    if start >= 0:
        rest = without_block(text[start:])
        return text[:start] + block + rest
    if text and not text.endswith("\n"):
        text += "\n"
    return text + ("\n" if text else "") + block


def shell_files(home):
    """(shell, startup file) for each shell set up here. fish gets a file of its own.
    Windows has none: it never edits a PowerShell profile."""
    if IS_WIN:
        return []
    out = []
    fish = home / ".config" / "fish"
    if fish.is_dir():
        out.append(("fish", fish / "conf.d" / "agent-resume.fish"))
    if (home / ".bashrc").is_file():
        out.append(("bash", home / ".bashrc"))
    zdot = Path(os.environ["ZDOTDIR"]) if os.environ.get("ZDOTDIR") and home == Path.home().resolve() else home
    if (zdot / ".zshrc").is_file():
        out.append(("zsh", zdot / ".zshrc"))
    return out


def shell_changes(home, script, mode):
    """(path, new text or None to delete) for the startup files. `mode`: "add", "remove",
    or "refresh" (update lines already there, add none)."""
    changes = []
    for shell, path in shell_files(home):
        old = read_text(path) if path.exists() else ""
        if mode == "remove" or (mode == "refresh" and BEGIN not in old):
            new = without_block(old)
        else:
            new = with_block(old, snippet(shell, script))
        if new == old:
            continue
        if shell == "fish" and not new.strip():
            new = None
        changes.append((path, new))
    return changes


def write_hook(path, config):
    """Write a hook file. A dedicated agent-resume.json that no longer has hooks is removed."""
    if path.name == "agent-resume.json" and vacant(config):
        path.unlink(missing_ok=True)
        return
    atomic_json(path, config)


def apply_shell(changes):
    for path, text in changes:
        if text is None:
            path.unlink(missing_ok=True)
        else:
            write_text(path, text, newline_of(path))


def verify_installed(destination, marker):
    if marker.get("package") != PACKAGE:
        raise ValueError("The existing mod belongs to another package")
    known = set(marker["files"]) | GENERATED | {MARKER}
    for path in destination.iterdir():
        if path.name not in known:
            raise ValueError(f"Extra installed file would be replaced: {path}. Preserve it before updating.")
    for filename, expected in marker["files"].items():
        path = destination / filename
        if not path.is_file() or digest(path) != expected:
            raise ValueError(f"Installed file was edited: {path}. Preserve that edit before updating.")


def agy_ours(block):
    if not isinstance(block, dict):
        return False
    for event in ("SessionStart", "PreInvocation"):
        for handler in block.get(event) or []:
            if isinstance(handler, dict) and HOOK_RE.search(str(handler.get("command", ""))):
                return True
    return False


def add_agy_hook(config, command):
    """Antigravity's hooks.json is a map of hook name to events, not Claude's `hooks` object."""
    merged = json.loads(json.dumps(config))
    current = merged.get("agent-resume")
    if current is not None and not agy_ours(current):
        raise ValueError("hooks.json already has an agent-resume hook that isn't this mod's. No files changed.")
    merged["agent-resume"] = {
        "SessionStart": [{"type": "command", "command": command, "timeout": 10}],
        "PreInvocation": [{"type": "command", "command": command, "timeout": 10}],
    }
    return merged


def remove_agy_hook(config):
    merged = json.loads(json.dumps(config))
    if agy_ours(merged.get("agent-resume")):
        del merged["agent-resume"]
    return merged


def _kiro_ours(hook):
    if not isinstance(hook, dict):
        return False
    if str(hook.get("name", "")).startswith("agent-resume-"):
        return True
    action = hook.get("action")
    return isinstance(action, dict) and HOOK_RE.search(str(action.get("command", ""))) is not None


def add_kiro_hook(config, command):
    """Kiro V3 global hooks apply to every profile, including the built-in agent."""
    merged = json.loads(json.dumps(config))
    hooks = merged.get("hooks")
    if hooks is None:
        hooks = []
    elif not isinstance(hooks, list):
        raise ValueError("Kiro hooks file `hooks` is not a list. No files changed.")
    kept = [hook for hook in hooks if not _kiro_ours(hook)]
    for trigger in ("SessionStart", "UserPromptSubmit"):
        kept.append({
            "name": f"agent-resume-{trigger}",
            "trigger": trigger,
            "action": {"type": "command", "command": command},
            "timeout": 10,
        })
    merged["hooks"] = kept
    merged.setdefault("version", "v1")
    return merged


def remove_kiro_hook(config):
    merged = json.loads(json.dumps(config))
    hooks = merged.get("hooks")
    if isinstance(hooks, list):
        merged["hooks"] = [hook for hook in hooks if not _kiro_ours(hook)]
    return merged


def hook_edit(kind, config, command, remove):
    if kind == "agy":
        return remove_agy_hook(config) if remove else add_agy_hook(config, command)
    if kind == "kiro":
        return remove_kiro_hook(config) if remove else add_kiro_hook(config, command)
    return remove_hook(config) if remove else add_hook(config, command)


def vacant(config):
    """True when a hook file we created would hold nothing but an empty hooks list."""
    if any(key not in ("hooks", "version") for key in config):
        return False
    hooks = config.get("hooks")
    return hooks in (None, {}, [])


def pi_extension(script):
    template = read_text(ROOT / "scripts" / "pi_extension.js")
    if "__SCRIPT__" not in template:
        raise ValueError("pi extension template has no __SCRIPT__ placeholder")
    return template.replace("__SCRIPT__", json.dumps(str(script)))


def pi_changes(home, script, remove):
    """(path, new text or None to delete) for Pi's extension, when Pi is set up here."""
    if IS_WIN:
        return []
    agent = home / ".pi" / "agent"
    if not agent.is_dir():
        return []
    path = agent / "extensions" / "herdr-agent-resume.js"
    if remove:
        if path.is_file() and "herdr-agent-resume pi session hook" in read_text(path):
            return [(path, None)]
        return []
    text = pi_extension(script)
    if path.is_file() and read_text(path) == text:
        return []
    return [(path, text)]


def agent_configs(home):
    """(name, path, existing config, kind) for each agent that is set up on this machine.

    `kind` is how that file lists hooks: "group" (Claude's SessionStart groups),
    "agy" (a named block) or "kiro" (a list of trigger entries)."""
    specs = (
        ("claude", ".claude", "settings.json", "group"),
        ("codex", ".codex", "hooks.json", "group"),
        ("grok", ".grok", "hooks/agent-resume.json", "group"),
        ("agy", ".gemini", "config/hooks.json", "agy"),
        ("kiro", ".kiro", "hooks/agent-resume.json", "kiro"),
    )
    out = []
    for name, folder, file, kind in specs:
        if IS_WIN and name not in WINDOWS_AGENT_NAMES:
            continue
        if not (home / folder).is_dir():
            continue
        path = home / folder / file
        config = read_json(path)
        if not isinstance(config, dict):
            raise ValueError(f"{path} is not a JSON object")
        out.append((name, path, config, kind))
    return out


# ---------------------------------------------------------------- Windows background watcher

def watcher_command(script):
    """The Run-key command: pythonw (no console window) running the watcher."""
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    exe = pythonw if pythonw.exists() else Path(sys.executable)
    return f'"{exe}" "{script}" auto-watch'


def registry_get():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            return winreg.QueryValueEx(key, RUN_VALUE)[0]
    except FileNotFoundError:
        return None


def registry_set(value):
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, value)


def registry_delete():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, RUN_VALUE)
    except FileNotFoundError:
        pass


def watcher_wanted(args, current):
    """The Run value to leave in place: None for none. Without --auto/--no-auto an
    existing registration is kept (and refreshed so it points at the installed script)."""
    if args.uninstall or args.no_auto:
        return None
    if args.auto:
        return "add"
    return "refresh" if current else None


# ---------------------------------------------------------------- backups

def backup_root(app):
    return app / "backups" / MOD_NAME


def list_backups(app):
    root = backup_root(app)
    return sorted((p for p in root.iterdir() if (p / "manifest.json").is_file()), key=lambda p: p.name) if root.is_dir() else []


def restore_backup(app, stamp, dry_run):
    """Put a backup back exactly: files that existed are rewritten, files the install
    created are removed, the mod folder and the Run value return to what they were."""
    backups = list_backups(app)
    if not backups:
        raise ValueError("No backups to restore.")
    chosen = backups[-1] if stamp in (None, "latest") else backup_root(app) / stamp
    if not (chosen / "manifest.json").is_file():
        raise ValueError(f"No such backup: {chosen.name}. Try --list-backups.")
    manifest = json.loads(read_text(chosen / "manifest.json"))
    actions = [f"restore {item['original']}" for item in manifest["files"]]
    actions += [f"remove {path}" for path in manifest["absent"]]
    if manifest.get("mod_backup"):
        actions.append(f"restore mod folder {manifest['mod_path']}")
    elif manifest.get("mod_path"):
        actions.append(f"remove mod folder {manifest['mod_path']}")
    if IS_WIN and "registry" in manifest:
        actions.append("restore the background watcher registration")
    print(f"Backup {chosen.name}:")
    for action in actions:
        print(f"  {action}")
    if dry_run:
        print("Dry run: no files changed.")
        return
    for item in manifest["files"]:
        target = Path(item["original"])
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(chosen / item["backup"], target)
    for path in manifest["absent"]:
        Path(path).unlink(missing_ok=True)
    mod_path = Path(manifest["mod_path"]) if manifest.get("mod_path") else None
    if mod_path and (manifest.get("mod_backup") or mod_path.exists()):
        if mod_path.exists():
            shutil.rmtree(mod_path)
        if manifest.get("mod_backup"):
            shutil.copytree(chosen / manifest["mod_backup"], mod_path)
    if IS_WIN and "registry" in manifest:
        previous = manifest["registry"]
        registry_set(previous) if previous else registry_delete()
    print(f"Restored backup {chosen.name}.")


# ---------------------------------------------------------------- main

def patched_main_js(source):
    """main.js with the interpreter name this platform uses."""
    text = read_text(source / "main.js")
    return text.replace('const PYTHON = "python3";', f'const PYTHON = "{"python" if IS_WIN else "python3"}";')


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--app-home", type=Path, default=Path(os.environ.get("HERDR_APP_HOME", Path.home() / ".config/herdr-app")))
    parser.add_argument("--home", type=Path, default=Path.home(), help="where ~/.claude, ~/.codex (and on Linux/macOS ~/.grok, ~/.gemini, ~/.pi, ~/.kiro) live")
    parser.add_argument("--update", action="store_true", help="update files previously installed by this script")
    parser.add_argument("--uninstall", action="store_true")
    parser.add_argument("--no-adopt", action="store_true", help="don't note Claude Code sessions already running")
    parser.add_argument("--auto", action="store_true", help="Windows: register the background watcher (still off until `agent_resume.py auto on`). Linux/macOS: shell startup lines")
    parser.add_argument("--no-auto", action="store_true", help="take back what --auto added")
    parser.add_argument("--dry-run", action="store_true", help="show what would change and change nothing")
    parser.add_argument("--list-backups", action="store_true", help="list the backups this installer made")
    parser.add_argument("--restore", nargs="?", const="latest", metavar="STAMP", help="restore a backup (default: the latest)")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    app = args.app_home.expanduser().resolve()
    home = args.home.expanduser().resolve()
    if args.list_backups:
        for path in list_backups(app):
            print(path.name)
        return
    if args.restore:
        restore_backup(app, args.restore, args.dry_run)
        return
    env = {**os.environ, "HERDR_APP_HOME": str(app)}
    keys_path = app / "keys.json"
    keys_snippet = json.loads(read_text(ROOT / "keys.json"))
    source = ROOT / "mods" / MOD_NAME
    destination = app / "mods" / MOD_NAME
    script = destination / "agent_resume.py"
    configs = agent_configs(home)
    if args.auto and args.no_auto:
        raise ValueError("--auto and --no-auto can't go together")
    mode = "remove" if args.uninstall or args.no_auto else "add" if args.auto else "refresh"
    shells = shell_changes(home, script, mode)
    pi = pi_changes(home, script, args.uninstall)
    registry_before = registry_get() if IS_WIN else None
    wanted = watcher_wanted(args, registry_before) if IS_WIN else None
    registry_after = watcher_command(script) if wanted else None

    if args.uninstall:
        keys = remove_keys(read_json(keys_path), keys_snippet)
        edited = [(path, hook_edit(kind, config, None, True)) for _, path, config, kind in configs]
    else:
        keys = merge_keys(read_json(keys_path), keys_snippet)
        edited = [(path, hook_edit(kind, config, hook_command(script, name), False)) for name, path, config, kind in configs]
        if destination.exists():
            marker_path = destination / MARKER
            if not args.update or not marker_path.exists():
                raise ValueError(f"{destination} already exists. Use --update only for this installer's own files.")
            verify_installed(destination, read_json(marker_path))
        subprocess.run(["herdr-app", "check", str(source)], env=env, check=True)
    changes = [(path, new) for (path, new), (_, _, old, _) in zip(edited, configs) if new != old]

    print(f"Mod: {destination}\nShortcut: Ctrl+Shift+R")
    for path, _ in changes:
        print(f"Hook: {path}")
    for path, text in pi:
        print(f"{'Pi extension removed' if text is None else 'Pi extension'}: {path}")
    for path, text in shells:
        print(f"{'Startup lines removed' if text is None or BEGIN not in text else 'Startup lines'}: {path}")
    if IS_WIN and registry_after != registry_before:
        print(f"Background watcher: {'registered' if registry_after else 'removed'} (HKCU\\{RUN_KEY}\\{RUN_VALUE}); it does nothing until `auto on`")
    hooked = {name for name, _, _, _ in configs}
    if args.dry_run:
        print("Dry run: no files changed.")
        return

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup = backup_root(app) / stamp
    backup.mkdir(parents=True)
    manifest = {"version": VERSION, "created": stamp, "files": [], "absent": [],
                "mod_path": str(destination), "mod_backup": None}

    def save(original, name):
        original = Path(original)
        if original.exists():
            shutil.copy2(original, backup / name)
            manifest["files"].append({"original": str(original), "backup": name})
        else:
            manifest["absent"].append(str(original))

    save(keys_path, "keys.json")
    for name, path, _, _ in configs:
        save(path, f"{name}-{path.name}")
    for path, _ in pi:
        save(path, f"pi-{path.name}")
    for path, _ in shells:
        save(path, f"shell-{path.name.lstrip('.')}")
    if destination.exists():
        shutil.copytree(destination, backup / MOD_NAME, ignore=shutil.ignore_patterns("__pycache__"))
        manifest["mod_backup"] = MOD_NAME
    if IS_WIN:
        manifest["registry"] = registry_before
    atomic_json(backup / "manifest.json", manifest)

    if args.uninstall:
        for path, config in changes:
            write_hook(path, config)
        atomic_json(keys_path, keys)
        apply_shell(pi)
        apply_shell(shells)
        if IS_WIN and registry_before:
            registry_delete()
        if destination.exists():
            shutil.rmtree(destination)
        print(f"Uninstalled. Backup: {backup}")
        return

    stage = Path(tempfile.mkdtemp(prefix=".agent-resume-stage-", dir=app))
    previous = app / f".agent-resume-previous-{stamp}"
    replaced = False
    try:
        for filename in FILES:
            if filename == "main.js":
                write_text(stage / filename, patched_main_js(source))
            else:
                shutil.copy2(source / filename, stage / filename)
        atomic_json(stage / MARKER, {
            "package": PACKAGE, "version": VERSION,
            "files": {filename: digest(stage / filename) for filename in FILES},
        })
        if destination.exists():
            destination.replace(previous)
        destination.parent.mkdir(parents=True, exist_ok=True)
        stage.replace(destination)
        replaced = True
        atomic_json(keys_path, keys)
        for path, config in changes:
            write_hook(path, config)
        apply_shell(pi)
        apply_shell(shells)
        if IS_WIN and registry_after != registry_before:
            registry_set(registry_after) if registry_after else registry_delete()
    except Exception:
        if replaced and destination.exists():
            shutil.rmtree(destination)
        if previous.exists():
            previous.replace(destination)
        restore_backup_files(backup, manifest)
        raise
    finally:
        if stage.exists():
            shutil.rmtree(stage)
        if previous.exists():
            shutil.rmtree(previous)
    print(f"Installed. Backup: {backup}")

    if not args.no_adopt:
        adopted = subprocess.run([sys.executable, str(script), "adopt"],
                                 env={k: v for k, v in env.items() if k not in ("HC_TERMINAL_ID", "HC_SCOPE")},
                                 capture_output=True, text=True, encoding="utf-8", errors="replace")
        try:
            for item in json.loads(adopted.stdout).get("adopted", []):
                print(f"Noted the running Claude Code session in {item['where'] or item['terminal']}")
        except ValueError:
            print(f"Couldn't note running sessions: {(adopted.stderr or adopted.stdout).strip()}")
    if "codex" in hooked and any(path.parent.name == ".codex" for path, _ in changes):
        print("Codex asks you to review the new hook the next time it starts: choose to trust it.")
    if "grok" in hooked and any(path.parent.name == "hooks" and path.name == "agent-resume.json" and ".grok" in path.parts for path, _ in changes):
        print("Grok runs the new hook after you trust the folder.")
    print("New sessions pick up the hook when they start; running ones keep their old hooks.")
    if IS_WIN and args.auto:
        print("Background watcher registered for your next sign-in. Automatic resume stays OFF; turn it on with:\n"
              f"  python \"{script}\" auto on")
    elif any(text and BEGIN in text for _, text in shells):
        print("Restored terminals now start their agent again by themselves; Ctrl+Shift+R still works for the rest.")
    elif args.no_auto:
        print("Restored terminals no longer start their agent by themselves; press Ctrl+Shift+R.")


def restore_backup_files(backup, manifest):
    """Undo a half-finished install from its own backup (used when a write fails)."""
    for item in manifest["files"]:
        shutil.copy2(backup / item["backup"], item["original"])
    for path in manifest["absent"]:
        Path(path).unlink(missing_ok=True)
    if IS_WIN and "registry" in manifest:
        registry_set(manifest["registry"]) if manifest["registry"] else registry_delete()


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        raise SystemExit(str(error)) from error
