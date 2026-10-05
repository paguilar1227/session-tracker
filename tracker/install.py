"""Install/uninstall: LaunchAgent, Codex hooks (hooks.json), MCP registrations."""
import json
import os
import plistlib
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import config

LABEL = config.LABEL
AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
PLIST = AGENTS_DIR / f"{LABEL}.plist"
# Settings the service needs that are chosen at install time; kept in the LaunchAgent so re-running the installer keeps them.
SERVICE_SETTINGS = ("ST_EXTRACT_MODEL", "ST_EXTRACT_EFFORT", "ST_EXTRACT_ENABLED", "ST_GH_USER", "ST_COPILOT_BASE",
                    "ST_COPILOT_INTEGRATION_ID", "CODEX_HOME")
HOOK_EVENTS = ("SessionStart", "SubagentStart", "SubagentStop", "Stop", "PostCompact", "PostToolUse")
# Codex shows statusMessage as the hook's name in its hooks settings ("1 - <statusMessage>") and while the hook runs.
HOOK_NAMES = {
    "SessionStart": "Session tracker: register session, load state",
    "SubagentStart": "Session tracker: register subagent",
    "SubagentStop": "Session tracker: record subagent result",
    "Stop": "Session tracker: record turn",
    "PostCompact": "Session tracker: restore state after compaction",
    "PostToolUse": "Session tracker: reload state into a compacted subagent",
}
HOOK_MARK = "/bin/st-hook"
MCP_NAME = "session_tracker"
# Generous ceiling over the hook's own 2-second service timeout, so Codex never waits long on it.
HOOK_TIMEOUT_SECONDS = 10


def python():
    return sys.executable


def hook_command(event=None):
    if event == "PostToolUse":  # runs on every tool call: a shell gate starts Python only while a reload is pending
        return f"'/bin/sh' '{config.PROJECT_DIR / 'bin' / 'st-hook-posttool'}' '{python()}'"
    return f"'{python()}' '{config.PROJECT_DIR / 'bin' / 'st-hook'}'"


def _codex():
    return shutil.which("codex") or str(Path.home() / ".local" / "bin" / "codex")


def _service_agents():
    """LaunchAgents that run this checkout's service, whatever their label (an install made under an earlier label)."""
    found = []
    for p in sorted(AGENTS_DIR.glob("*.plist")):
        try:
            d = plistlib.loads(p.read_bytes())
        except (OSError, ValueError, plistlib.InvalidFileException):
            continue
        if d.get("ProgramArguments", [])[1:] == ["-m", "tracker", "serve"] and d.get("WorkingDirectory") == str(config.PROJECT_DIR):
            found.append((p, d))
    return found


def _bootout(label):
    """Stop a LaunchAgent and wait until launchd has removed it (bootstrapping again before that fails with error 5)."""
    target = f"gui/{os.getuid()}/{label}"
    r = subprocess.run(["launchctl", "bootout", target], capture_output=True, text=True)
    loaded = lambda: subprocess.run(["launchctl", "print", target], capture_output=True).returncode == 0
    if r.returncode != 0 and loaded():
        raise SystemExit(f"launchctl bootout {target} failed: {r.stderr.strip()}")
    while loaded():
        time.sleep(0.2)


def _retire_other_labels():
    """Stop and remove agents for this checkout registered under another label, so only one service runs."""
    retired = []
    for p, d in _service_agents():
        if d.get("Label") != LABEL:
            _bootout(d.get("Label"))
            p.unlink()
            retired.append(d.get("Label"))
    return retired


def _settings(previous):
    """Service settings from this install's environment, else from the agent being replaced."""
    keep = {k: v for k, v in (previous or {}).items() if k in SERVICE_SETTINGS}
    keep.update({k: os.environ[k] for k in SERVICE_SETTINGS if os.environ.get(k)})
    return keep


def install_launch_agent():
    config.ensure_dirs()
    previous = next((d.get("EnvironmentVariables") for _, d in _service_agents()), None)
    retired = _retire_other_labels()
    env = {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(Path.home())}
    env.update(_settings(previous))
    plist = {"Label": LABEL, "ProgramArguments": [python(), "-m", "tracker", "serve"],
             "WorkingDirectory": str(config.PROJECT_DIR), "EnvironmentVariables": env,
             "RunAtLoad": True, "KeepAlive": True,
             "StandardOutPath": str(config.LOG_DIR / "launchd.out.log"), "StandardErrorPath": str(config.LOG_DIR / "launchd.err.log")}
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    new = plistlib.dumps(plist)
    unchanged = PLIST.exists() and PLIST.read_bytes() == new
    PLIST.write_bytes(new)
    domain, target = f"gui/{os.getuid()}", f"gui/{os.getuid()}/{LABEL}"

    def loaded():
        return subprocess.run(["launchctl", "print", target], capture_output=True).returncode == 0
    if unchanged and loaded():
        subprocess.run(["launchctl", "kickstart", "-k", target], capture_output=True)  # same definition: just restart
        return str(PLIST), retired
    if loaded():
        _bootout(LABEL)  # bootout returns before launchd has finished; bootstrapping too early fails with error 5
    r = subprocess.run(["launchctl", "bootstrap", domain, str(PLIST)], capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"launchctl bootstrap failed: {r.stderr.strip()}")
    return str(PLIST), retired


def hooks_path():
    return config.CODEX_HOME / "hooks.json"


def _snake(event):
    return "".join("_" + ch.lower() if ch.isupper() else ch for ch in event).lstrip("_")


def hook_trust():
    """How many of our hooks Codex has a trust record for (reads only the [hooks.state] keys of config.toml)."""
    import tomllib
    try:
        data = json.loads(hooks_path().read_text())
        with open(config.CODEX_HOME / "config.toml", "rb") as f:
            state = (tomllib.load(f).get("hooks") or {}).get("state") or {}
    except (OSError, ValueError):
        return {"installed": 0, "trusted": 0}
    installed = trusted = 0
    for event, groups in (data.get("hooks") or {}).items():
        for gi, g in enumerate(groups):
            for hi, h in enumerate(g.get("hooks", [])):
                if HOOK_MARK in str(h.get("command", "")):
                    installed += 1
                    if (state.get(f"{hooks_path()}:{_snake(event)}:{gi}:{hi}") or {}).get("trusted_hash"):
                        trusted += 1
    return {"installed": installed, "trusted": trusted}


def _strip_ours(data):
    hooks = data.setdefault("hooks", {})
    for event, groups in list(hooks.items()):
        kept = []
        for g in groups:
            g = dict(g)
            g["hooks"] = [h for h in g.get("hooks", []) if HOOK_MARK not in str(h.get("command", ""))]
            if g["hooks"]:
                kept.append(g)
        if kept:
            hooks[event] = kept
        else:
            hooks.pop(event)
    return data


def install_hooks():
    path = hooks_path()
    data = json.loads(path.read_text()) if path.exists() else {"hooks": {}}
    if path.exists():
        backup = path.with_name(f"hooks.json.bak-session-tracker-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(path, backup)
    data = _strip_ours(data)
    for event in HOOK_EVENTS:
        data["hooks"].setdefault(event, []).append(
            {"hooks": [{"type": "command", "command": hook_command(event), "timeout": HOOK_TIMEOUT_SECONDS, "statusMessage": HOOK_NAMES[event]}]})
    path.write_text(json.dumps(data, indent=2) + "\n")
    _remember_hooks_changed(_ours(data))
    return str(path)


def _ours(data):
    """Our hook entries with their positions; Codex keys hook trust by file, event, group and index plus the definition."""
    return sorted((event, gi, hi, json.dumps(h, sort_keys=True)) for event, groups in (data.get("hooks") or {}).items()
                  for gi, g in enumerate(groups) for hi, h in enumerate(g.get("hooks", [])) if HOOK_MARK in str(h.get("command", "")))


def _remember_hooks_changed(ours):
    """Codex skips new or changed hook definitions until they are approved in /hooks, so the UI warns until one of ours has run
    since they last changed. Re-installing identical definitions (even after an uninstall) is not a change: the last installed
    definitions are kept in the tracker database, which uninstall leaves in place."""
    from . import db
    config.ensure_dirs()
    c = db.connect()
    try:
        db.init(c)
        current = json.dumps(ours)
        if db.get_meta("hooks_definitions", None, c) != current:
            with db.write(c):
                db.set_meta("hooks_definitions", current, c)
                db.set_meta("hooks_changed_at", db.now_ms(), c)
    finally:
        c.close()


def uninstall_hooks():
    path = hooks_path()
    if not path.exists():
        return
    shutil.copy2(path, path.with_name(f"hooks.json.bak-session-tracker-{time.strftime('%Y%m%d-%H%M%S')}"))
    path.write_text(json.dumps(_strip_ours(json.loads(path.read_text())), indent=2) + "\n")


def _mcp_exists(name):
    r = subprocess.run([_codex(), "mcp", "get", name], capture_output=True, text=True)
    return r.returncode == 0


def install_mcp(with_canvas=False):
    done = []
    if not _mcp_exists(MCP_NAME):
        subprocess.run([_codex(), "mcp", "add", MCP_NAME, "--", python(), str(config.PROJECT_DIR / "bin" / "st-mcp")], check=True,
                       capture_output=True)
        done.append(MCP_NAME)
    if with_canvas and not _mcp_exists("workflow-canvas"):
        subprocess.run([_codex(), "mcp", "add", "workflow-canvas", "--url", f"{config.WORKFLOW_CANVAS_URL}/mcp"], check=True,
                       capture_output=True)
        done.append("workflow-canvas")
    return done


def approve_mcp_tools(name=MCP_NAME):
    """Let sessions call the tracker's tools without an approval prompt (they only touch the local tracker; ledger deletes
    are not exposed and delete_todo is scoped). Sets default_tools_approval_mode = "approve" in the server's config.toml table."""
    path = config.CODEX_HOME / "config.toml"
    if not path.exists():
        return False
    lines = path.read_text().splitlines(keepends=True)
    header = f"[mcp_servers.{name}]"
    try:
        start = next(i for i, ln in enumerate(lines) if ln.strip() == header)
    except StopIteration:
        return False
    end = next((i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith("[")), len(lines))
    if any(ln.split("=")[0].strip() == "default_tools_approval_mode" for ln in lines[start + 1:end]):
        return False
    shutil.copy2(path, path.with_name(f"config.toml.bak-session-tracker-{time.strftime('%Y%m%d-%H%M%S')}"))
    lines.insert(start + 1, 'default_tools_approval_mode = "approve"\n')
    path.write_text("".join(lines))
    return True


def skill_path():
    return config.CODEX_HOME / "skills" / "session-tracker"


def install_skill():
    """Link the skill that tells sessions when to read and write the tracker. An existing skill of the same name is kept."""
    dst = skill_path()
    if dst.is_symlink() and dst.resolve() == config.SKILL_SRC.resolve():
        return "linked"
    if dst.exists() or dst.is_symlink():
        return f"kept the existing {dst}"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.symlink_to(config.SKILL_SRC)
    return "linked"


def uninstall_skill():
    dst = skill_path()
    if dst.is_symlink() and dst.resolve() == config.SKILL_SRC.resolve():
        dst.unlink()


def install(with_canvas=False):
    agent, retired = install_launch_agent()
    hooks = install_hooks()
    mcp = install_mcp(with_canvas)
    approve_mcp_tools()
    skill = install_skill()
    print(f"LaunchAgent: {agent} ({LABEL})" + (f"; replaced {', '.join(retired)}" if retired else "") + "\n"
          f"Hooks added to: {hooks} ({', '.join(HOOK_EVENTS)})\n"
          f"MCP servers added: {', '.join(mcp) or 'already present'}\nSkill: {skill} ({skill_path()})\nUI: {config.BASE_URL}\n\n"
          "One manual step: open Codex, run /hooks (or Settings > Hooks) and approve the hooks named Session tracker.\n"
          "Until then the tree, statuses, todos, artifacts and ledger still work; digest injection after compaction does not.")


def uninstall():
    _bootout(LABEL)
    if PLIST.exists():
        PLIST.unlink()
    _retire_other_labels()
    uninstall_hooks()
    uninstall_skill()
    subprocess.run([_codex(), "mcp", "remove", MCP_NAME], capture_output=True)
    print(f"Stopped the service and removed its LaunchAgent, the session-tracker hooks (a backup of hooks.json is kept next to it), "
          f"the skill link and the {MCP_NAME} MCP server. Data kept in {config.DATA_DIR}."
          + (" The workflow-canvas MCP registration was left in place." if _mcp_exists("workflow-canvas") else ""))

