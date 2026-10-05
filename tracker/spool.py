"""Hook events are appended to a local spool first, then ingested by the service.

Writing to a file never depends on the service being up, so no hook event is lost.
"""
import datetime as dt
import fcntl
import json
import os

from . import config, db

KEEP_TEXT = 4000


def append(event):
    config.ensure_dirs()
    path = config.SPOOL_DIR / f"{dt.datetime.now():%Y%m%d}.jsonl"
    line = json.dumps(event, ensure_ascii=False) + "\n"
    with open(path, "a", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.write(line)
            f.flush()
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
    return path


def _ensure_session(c, session_id, parent_id=None, cwd=None):
    if not session_id:
        return False
    now = db.now_ms()
    cur = c.execute("""insert into sessions(id,parent_id,root_id,depth,status,created_at,updated_at,cwd,last_hook_at)
                       values(?,?,?,?, 'running', ?, ?, ?, ?) on conflict(id) do update set last_hook_at=excluded.last_hook_at""",
                    (session_id, parent_id, parent_id or session_id, 1 if parent_id else 0, now, now, cwd, now))
    return cur.rowcount > 0


def _trim(payload):
    p = dict(payload)
    for k, v in list(p.items()):
        if isinstance(v, str) and len(v) > KEEP_TEXT:
            p[k] = v[:KEEP_TEXT] + "…"
    return p


def ingest(c):
    """Read new spool lines. Returns number of events ingested."""
    if not config.SPOOL_DIR.exists():
        return 0
    count = 0
    for path in sorted(config.SPOOL_DIR.glob("*.jsonl")):
        key = f"spool:{path.name}"
        offset = int(db.get_meta(key, "0", c))
        size = path.stat().st_size
        if size <= offset:
            continue
        with open(path, "rb") as f:
            f.seek(offset)
            data = f.read()
        end = data.rfind(b"\n")
        if end < 0:
            continue
        lines = data[:end + 1].decode("utf-8", "replace").splitlines()
        with db.write(c):
            for line in lines:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                p = ev.get("payload") or {}
                name = p.get("hook_event_name") or ev.get("event") or "unknown"
                sid = p.get("session_id")
                if name == "SubagentStart" and p.get("agent_id"):
                    _ensure_session(c, p["agent_id"], sid, p.get("cwd"))
                    db.log_event(c, "hook", name, p["agent_id"], _trim(p))
                elif name == "SubagentStop" and p.get("agent_id"):
                    _ensure_session(c, p["agent_id"], sid, p.get("cwd"))
                    db.log_event(c, "hook", name, p["agent_id"], _trim(p))
                else:
                    _ensure_session(c, sid, None, p.get("cwd"))
                    db.log_event(c, "hook", name, sid, _trim(p))
                count += 1
            db.set_meta(key, offset + end + 1, c)
    return count

