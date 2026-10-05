"""python -m tracker <serve|hook|mcp|install|uninstall|status|copilot [--no-live]|extract SESSION_ID|recheck [--missing-lessons | LEDGER_ID...]|export-feedback DIR>"""
import json
import os
import sys


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "help"
    if cmd == "serve":
        from .service import serve
        return serve()
    if cmd == "hook":
        from .hook import main as hook_main
        return hook_main()
    if cmd == "mcp":
        from .mcp_server import main as mcp_main
        return mcp_main()
    if cmd == "install":
        from .install import install
        return install(with_canvas="--with-canvas" in argv)
    if cmd == "uninstall":
        from .install import uninstall
        return uninstall()
    if cmd == "status":
        import urllib.request
        from . import config
        try:
            with urllib.request.urlopen(config.BASE_URL + "/api/health", timeout=5) as r:
                print(json.dumps(json.load(r), indent=2))
        except OSError as e:
            print(f"service not reachable at {config.BASE_URL}: {e}")
            return 1
        return 0
    if cmd == "copilot":
        return copilot_check(live="--no-live" not in argv)
    if cmd == "extract" and len(argv) > 1:
        import urllib.request
        from . import config
        req = urllib.request.Request(f"{config.BASE_URL}/api/sessions/{argv[1]}/extract", data=b'{"only_missing": true}',
                                     method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            print(json.dumps(json.load(r)))
        return 0
    if cmd == "export-feedback" and len(argv) > 1:
        from . import db
        from .feedback_export import export
        db.init()
        print(json.dumps(export(argv[1])))
        return 0
    if cmd == "recheck" and len(argv) > 1:
        from . import db, extractor
        from .codex_source import CodexSource
        db.init()
        c = db.conn()
        ids = [int(x) for x in argv[1:] if x.isdigit()]
        if "--missing-lessons" in argv:
            ids += [r[0] for r in c.execute("""select id from ledger l where kind='mistake' and coalesce(trim(lesson), '') = ''
                                               and not exists (select 1 from corrections x where x.ledger_id = l.id and coalesce(x.lesson, '') != '')""")]
        client, src = extractor.Copilot(), CodexSource()
        for i in ids:
            try:
                r = extractor.recheck_mistake(c, client, src, i)
                print(i, r["verdict"], (r.get("correction") or {}).get("lesson") or "")
            except (ValueError, KeyError, extractor.ExtractError) as e:
                print(i, "skipped:", e)
        return 0
    print(__doc__)
    return 0


SETTING_ATTRS = {"ST_EXTRACT_MODEL": "EXTRACT_MODEL", "ST_EXTRACT_EFFORT": "EXTRACT_EFFORT", "ST_GH_USER": "GH_USER",
                 "ST_COPILOT_BASE": "COPILOT_BASE", "ST_COPILOT_INTEGRATION_ID": "COPILOT_INTEGRATION_ID"}


def copilot_check(live=True):
    """Check the scribe's GitHub Copilot connection and say how to fix what is missing. Exit code 1 when it can't run."""
    from . import config, extractor, install
    installed = next((d.get("EnvironmentVariables") or {} for _, d in install._service_agents()), {})
    for key, attr in SETTING_ATTRS.items():
        if key not in os.environ and installed.get(key):
            setattr(config, attr, installed[key])
    if os.environ.get("ST_EXTRACT_ENABLED", installed.get("ST_EXTRACT_ENABLED", "1")) == "0":
        print("Scribe: disabled (ST_EXTRACT_ENABLED=0); skipping the Copilot check.")
        return 0
    rerun = "then re-run ./install.sh"
    try:
        r = extractor.Copilot().check(live=live)
    except extractor.ExtractError as e:
        msg = str(e)
        if "GitHub token" in msg and config.GH_USER:
            print(f"Copilot: gh has no signed-in account {config.GH_USER!r} ({msg}).\n  Fix: gh auth login as that account, "
                  f"or re-run ./install.sh with ST_GH_USER set to an account listed by gh auth status.")
        elif "GitHub token" in msg:
            print(f"Copilot: gh is not installed or not signed in ({msg}).\n  Fix: brew install gh && gh auth login, {rerun}.")
        elif "copilot_internal" in msg:
            print(f"Copilot: this GitHub account has no Copilot access ({msg}).\n  Fix: sign in to an account with Copilot "
                  f"(gh auth login), or pick one you are already signed in to: ST_GH_USER=<login> ./install.sh")
        else:
            print(f"Copilot: {msg}")
        print("  Until then the scribe stays idle; the session tree, todos, artifacts and UI still work.")
        return 1
    who = r["login"] + (" (ST_GH_USER)" if config.GH_USER else " (gh's active account)")
    print(f"GitHub account: {who}\nCopilot plan: {r['plan']} · API {r['base']}")
    if not r["model_ok"]:
        claude = [m for m in r["models"] if m.startswith("claude")]
        print(f"Scribe model: {r['model']} is not available to this account through Copilot's chat completions API, which the "
              f"scribe uses.\n  Fix: ST_EXTRACT_MODEL=<model> ./install.sh "
              f"(the scribe's prompts were tuned on Claude Sonnet). Available: {', '.join(claude or r['models']) or 'none'}")
        return 1
    if live:
        print(f"Scribe model: {r['model']} answered {r['reply']!r} in {r['seconds']}s")
    else:
        print(f"Scribe model: {r['model']} is available")
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    sys.exit(main() or 0)

