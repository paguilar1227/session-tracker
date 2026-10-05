"""SQLite store: schema, connection handling, change counter and full-text index."""
import json
import sqlite3
import threading
import time

from . import config

TODO_STATES = ("not_started", "in_progress", "review", "blocked", "waiting", "done")
PRIORITIES = ("P1", "P2", "P3")
LEDGER_KINDS = ("decision", "mistake", "issue")
LEDGER_STATUS = {
    "decision": ("proposed", "decided", "superseded"),
    "mistake": ("recorded",),
    "issue": ("open", "resolved"),
}
ATTACHMENT_KINDS = ("url", "artifact", "reference")
CORRECTION_SOURCES = ("user", "scribe")
FEEDBACK_KINDS = ("wrong", "missed")
# Which ledger fields can change after an entry is written. Mistakes are permanent lessons.
LEDGER_EDITABLE = {
    "decision": ("title", "body", "rationale", "alternatives", "status", "made_by"),
    "issue": ("title", "body", "status"),
    "mistake": (),
}

SCHEMA = """
create table if not exists meta(key text primary key, value text);
create table if not exists sessions(
  id text primary key, parent_id text, root_id text, depth integer not null default 0,
  agent_path text, nickname text, role text, title text, first_message text,
  cwd text, model text, effort text, source text, edge_status text,
  status text not null default 'new', archived integer not null default 0,
  created_at integer, updated_at integer, last_turn_at integer, turn_count integer not null default 0,
  artifacts_dir text, git_branch text, rollout_path text, last_hook_at integer, git_origin_url text, pinned_at integer,
  task text, result text
);
create index if not exists idx_sessions_parent on sessions(parent_id);
create index if not exists idx_sessions_root on sessions(root_id);
create index if not exists idx_sessions_updated on sessions(updated_at desc);
create table if not exists turns(
  session_id text not null, turn_id text not null, ordinal integer, status text,
  started_at integer, completed_at integer, duration_ms integer, error text,
  extract_status text not null default 'skipped', extract_error text, extracted_at integer,
  primary key(session_id, turn_id)
);
create index if not exists idx_turns_extract on turns(extract_status, completed_at);
create table if not exists todos(
  id integer primary key, session_id text not null, title text not null, notes text not null default '',
  priority text not null default 'P2', state text not null default 'not_started',
  created_at integer not null, updated_at integer not null, created_by text not null default 'user', source_ref text,
  position integer
);
create index if not exists idx_todos_session on todos(session_id);
create table if not exists todo_attachments(
  id integer primary key, todo_id integer not null references todos(id) on delete cascade,
  kind text not null, url text, artifact_id integer, title text, created_at integer not null
);
create index if not exists idx_attach_todo on todo_attachments(todo_id);
create table if not exists ledger(
  id integer primary key, session_id text not null, kind text not null, title text not null,
  body text not null default '', status text, made_by text, rationale text, alternatives text,
  what_worked text, lesson text, evidence text, turn_id text, source text not null default 'agent', model text,
  created_at integer not null, updated_at integer not null
);
create index if not exists idx_ledger_session on ledger(session_id, kind);
create table if not exists artifacts(
  id integer primary key, session_id text not null, path text, kind text not null, title text,
  size integer, mtime integer, origin text not null, canvas_id text, missing integer not null default 0,
  created_at integer not null, pinned_at integer
);
create unique index if not exists idx_artifacts_path on artifacts(session_id, path) where path is not null;
create unique index if not exists idx_artifacts_canvas on artifacts(session_id, canvas_id) where canvas_id is not null;
create table if not exists events(
  id integer primary key, ts integer not null, source text not null, type text not null,
  session_id text, payload text
);
create index if not exists idx_events_session on events(session_id, ts);
create table if not exists corrections(
  id integer primary key, ledger_id integer not null references ledger(id) on delete cascade, session_id text not null,
  cause text, lesson text, note text, source text not null, turn_id text, evidence text, model text, created_at integer not null
);
create index if not exists idx_corrections_ledger on corrections(ledger_id, id);
create table if not exists feedback(
  id integer primary key, session_id text not null, kind text not null, ledger_id integer, turn_id text,
  note text not null default '', created_at integer not null
);
create index if not exists idx_feedback_session on feedback(session_id);
create virtual table if not exists search_fts using fts5(kind unindexed, ref_id unindexed, session_id unindexed, title, body, tokenize='porter unicode61');
"""

_local = threading.local()
_write_lock = threading.RLock()
_version = {"n": 0}
_version_cond = threading.Condition()


def now_ms():
    return int(time.time() * 1000)


def connect(path=None):
    conn = sqlite3.connect(str(path or config.DB_PATH), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma journal_mode=wal")
    conn.execute("pragma foreign_keys=on")
    conn.execute("pragma busy_timeout=30000")
    return conn


def conn():
    """One connection per thread (re-opened if the configured database path changes)."""
    c = getattr(_local, "conn", None)
    if c is None or getattr(_local, "path", None) != str(config.DB_PATH):
        config.ensure_dirs()
        c = connect()
        _local.conn = c
        _local.path = str(config.DB_PATH)
    return c


def close_thread():
    """Close this thread's connection (HTTP request threads are short-lived; an unclosed WAL connection keeps its files open)."""
    c = getattr(_local, "conn", None)
    _local.conn = None
    _local.path = None
    if c is not None:
        c.close()


def init(c=None):
    c = c or conn()
    with _write_lock:
        c.executescript(SCHEMA)
        if "lesson" not in {r[1] for r in c.execute("pragma table_info(ledger)")}:
            c.execute("alter table ledger add column lesson text")
        if "git_origin_url" not in {r[1] for r in c.execute("pragma table_info(sessions)")}:
            c.execute("alter table sessions add column git_origin_url text")
        for table, col, typ in (("sessions", "pinned_at", "integer"), ("artifacts", "pinned_at", "integer"), ("todos", "position", "integer"),
                                ("sessions", "task", "text"), ("sessions", "result", "text")):
            if col not in {r[1] for r in c.execute(f"pragma table_info({table})")}:
                c.execute(f"alter table {table} add column {col} {typ}")
        if get_meta("task_reread_v1", c=c) is None:
            c.execute("update sessions set task=null where task=''")
            set_meta("task_reread_v1", now_ms(), c)
        backfill_todo_positions(c)
        c.commit()


def backfill_todo_positions(c):
    """Give todos written before manual ordering existed a position that keeps the order they were shown in."""
    for (sid,) in c.execute("select distinct session_id from todos where position is null").fetchall():
        ids = [r[0] for r in c.execute("""select id from todos where session_id=? order by case state when 'done' then 1 else 0 end,
                                          priority, position is null, position, updated_at desc""", (sid,))]
        for pos, tid in enumerate(ids, 1):
            c.execute("update todos set position=? where id=?", (pos, tid))


def bump():
    with _version_cond:
        _version["n"] += 1
        _version_cond.notify_all()


def version():
    return _version["n"]


def wait_for_change(seen, timeout):
    with _version_cond:
        _version_cond.wait_for(lambda: _version["n"] != seen, timeout=timeout)
        return _version["n"]


class write:
    """Serialized write transaction; bumps the change counter on success."""

    def __init__(self, c=None):
        self.c = c or conn()

    def __enter__(self):
        _write_lock.acquire()
        self.before = self.c.total_changes
        return self.c

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self.c.commit()
                if self.c.total_changes != self.before:
                    bump()
            else:
                self.c.rollback()
        finally:
            _write_lock.release()
        return False


def get_meta(key, default=None, c=None):
    row = (c or conn()).execute("select value from meta where key=?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(key, value, c):
    c.execute("insert into meta(key,value) values(?,?) on conflict(key) do update set value=excluded.value", (key, str(value)))


def log_event(c, source, type_, session_id=None, payload=None):
    c.execute("insert into events(ts,source,type,session_id,payload) values(?,?,?,?,?)",
              (now_ms(), source, type_, session_id, json.dumps(payload) if payload is not None else None))


def index_doc(c, kind, ref_id, session_id, title, body=""):
    c.execute("delete from search_fts where kind=? and ref_id=?", (kind, str(ref_id)))
    c.execute("insert into search_fts(kind,ref_id,session_id,title,body) values(?,?,?,?,?)",
              (kind, str(ref_id), session_id, title or "", body or ""))


def unindex_doc(c, kind, ref_id):
    c.execute("delete from search_fts where kind=? and ref_id=?", (kind, str(ref_id)))


def rows(cursor):
    return [dict(r) for r in cursor.fetchall()]

