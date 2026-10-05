"""Codex hook entry point. Must never block or fail a session.

1. Append the event to the local spool (works even if the service is down).
2. For SessionStart / SubagentStart, print the digest as additionalContext.
3. After compaction, reload the thread's state on its next tool call (see restore.py).
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request

from . import config, restore, spool

TIMEOUT = float(os.environ.get("ST_HOOK_TIMEOUT", "2"))


def _digest(session_id, event, source=None, parent=None, reserve=0):
    q = {"session": session_id, "event": event}
    if reserve:
        q["reserve"] = reserve
    if source:
        q["source"] = source
    if parent:
        q["parent"] = parent
    url = f"{config.BASE_URL}/api/digest?{urllib.parse.urlencode(q)}"
    with urllib.request.urlopen(url, timeout=TIMEOUT) as r:
        return json.load(r)["text"]


def fallback(session_id, event, parent=None):
    who = f"subagent session {session_id}" + (f" (spawned by {parent})" if parent else "") if event == "SubagentStart" else f"session {session_id}"
    return (f"[session-tracker] This is {who}. The tracker service is not reachable right now; "
            "the session_tracker MCP tools will work once it is back.")


RESTORED = "[session-tracker] Your context was just compacted. Reloaded state for this thread:\n"


def respond(payload):
    event = payload.get("hook_event_name")
    thread = payload.get("agent_id") or payload.get("session_id")
    if event == "PostCompact":
        restore.mark(thread)
        return None
    if event == "PostToolUse":
        if not thread or not restore.claim(thread):
            return None
        try:
            text = RESTORED + _digest(thread, "SessionStart", "compact", reserve=len(RESTORED))
        except Exception:
            text = (f"[session-tracker] Your context was just compacted. This is thread {thread}; call the session_tracker "
                    "get_session tool to reload its todos, decisions and lessons before continuing.")
        return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}
    if event == "SessionStart":
        sid = payload.get("session_id")
        if payload.get("source") == "compact":
            restore.clear(sid)
        try:
            text = _digest(sid, event, payload.get("source"))
        except Exception:
            text = fallback(sid, event)
        return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}
    if event == "SubagentStart":
        child, parent = payload.get("agent_id"), payload.get("session_id")
        try:
            text = _digest(child, event, None, parent)
        except Exception:
            text = fallback(child, event, parent)
        return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}
    return None


def _spool(payload):
    try:
        spool.append({"received_at": int(time.time() * 1000), "payload": payload})
    except Exception as e:
        sys.stderr.write(f"session-tracker: spool write failed: {e}\n")


def main():
    if os.environ.get("ST_DISABLE"):  # run Codex without the tracker (A/B tests): no spool, no output
        sys.stdin.read()
        return 0
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError:
        payload = {}
    event = payload.get("hook_event_name")
    if event != "PostToolUse":
        _spool(payload)
    try:
        out = respond(payload)
    except Exception:
        out = None
    if event == "PostToolUse" and out:
        _spool(dict(payload, st_restored=True))
    if out:
        sys.stdout.write(json.dumps(out))
    return 0

