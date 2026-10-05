"""A stand-in for Codex's own SQLite records (threads, spawn edges, turns, items), for tests and the demo."""
import json
import sqlite3
import time

NOW = int(time.time())


class FakeCodex:
    def __init__(self, root):
        root.mkdir(parents=True, exist_ok=True)
        self.state = root / "state_5.sqlite"
        self.history = root / "thread_history_1.sqlite"
        s = sqlite3.connect(self.state)
        s.execute("pragma journal_mode=wal")  # Codex's real databases are WAL; the leak only shows up in WAL mode
        s.executescript("""
        create table threads(id text primary key, rollout_path text, created_at int, updated_at int, source text, cwd text,
          title text, archived int default 0, first_user_message text default '', agent_nickname text, agent_role text,
          model text, reasoning_effort text, agent_path text, created_at_ms int, updated_at_ms int, git_branch text, thread_source text);
        create table thread_spawn_edges(parent_thread_id text, child_thread_id text primary key, status text);""")
        s.commit()
        s.close()
        h = sqlite3.connect(self.history)
        h.execute("pragma journal_mode=wal")
        h.executescript("""
        create table thread_turns(thread_id text, turn_id text, rollout_ordinal int, status text, error_json text,
          started_at int, completed_at int, duration_ms int, primary key(thread_id, turn_id));
        create table thread_items(thread_id text, turn_id text, item_id text, rollout_ordinal int, created_at_ms int,
          item_json text, item_type text, primary key(thread_id, turn_id, item_id));""")
        h.commit()
        h.close()
        self.ordinal = 0

    def thread(self, tid, title, parent=None, updated=None, archived=0, nickname=None, path=None, cwd="/tmp", created=None,
               rollout=None, model="gpt-6.1-sol"):
        updated = updated or NOW
        created = created or NOW - 100
        with sqlite3.connect(self.state) as s:
            s.execute("""insert or replace into threads(id,rollout_path,created_at,updated_at,source,cwd,title,archived,first_user_message,
                         agent_nickname,agent_path,model,created_at_ms,updated_at_ms,thread_source) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (tid, rollout or f"/x/{tid}.jsonl", created, updated, "vscode", cwd, title, archived, title, nickname, path,
                       model, created * 1000, updated * 1000, "subagent" if parent else "user"))
            if parent:
                s.execute("insert or replace into thread_spawn_edges values(?,?,?)", (parent, tid, "open"))

    def turn(self, tid, turn_id, status, completed=None, error=None):
        self.ordinal += 1
        with sqlite3.connect(self.history) as h:
            h.execute("insert or replace into thread_turns values(?,?,?,?,?,?,?,?)",
                      (tid, turn_id, self.ordinal, status, json.dumps({"message": error}) if error else None,
                       NOW - 50, completed, 1000))

    def item(self, tid, turn_id, item_id, item_type, obj):
        self.ordinal += 1
        with sqlite3.connect(self.history) as h:
            h.execute("insert or replace into thread_items values(?,?,?,?,?,?,?)",
                      (tid, turn_id, item_id, self.ordinal, int(time.time() * 1000), json.dumps(obj), item_type))

    def touch(self, tid, when):
        with sqlite3.connect(self.state) as s:
            s.execute("update threads set updated_at=?, updated_at_ms=? where id=?", (when, when * 1000, tid))
