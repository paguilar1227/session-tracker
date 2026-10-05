"""Read-only access to Codex's own SQLite records (threads, spawn edges, turns, items).

These are Codex internals, so every cycle starts with a schema check; a mismatch is
reported as unhealthy instead of guessed around.
"""
import json
import contextlib
import re
import sqlite3

from . import config, projects

STATE_REQUIRED = {
    "threads": {"id", "rollout_path", "created_at", "updated_at", "source", "cwd", "title", "archived",
                "first_user_message", "agent_nickname", "agent_role", "model", "reasoning_effort", "agent_path",
                "created_at_ms", "updated_at_ms", "git_branch"},
    "thread_spawn_edges": {"parent_thread_id", "child_thread_id", "status"},
}
HISTORY_REQUIRED = {
    "thread_turns": {"thread_id", "turn_id", "rollout_ordinal", "status", "error_json", "started_at",
                     "completed_at", "duration_ms"},
    "thread_items": {"thread_id", "turn_id", "item_id", "rollout_ordinal", "item_json", "item_type", "created_at_ms"},
}
TERMINAL = ("completed", "failed", "interrupted")


def _open(path):
    """Read-only connection that is really closed on exit.

    A bare "with sqlite3.connect()" only commits; on Codex's WAL databases the file descriptors then linger and
    exhaust launchd's 256-file soft limit within one backfill.
    """
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    c.row_factory = sqlite3.Row
    return contextlib.closing(c)


def _columns(c, table):
    return {r[1] for r in c.execute(f"pragma table_info({table})")}


NEW_TASK = re.compile(r"Message Type: NEW_TASK.*?\nPayload:\n(.*)", re.S)


def spawn_task(rollout_path, agent_path=None):
    """The task a subagent was spawned with: the payload of the first NEW_TASK message addressed to it.

    Returns None while the rollout does not hold it yet (the first reconcile can run before the message is written),
    so the caller reads again later. With an agent path, only a message addressed to that path counts, which skips
    the parent history a forked subagent's rollout starts with. Without one, reading stops at the first model output."""
    if not rollout_path or projects.protected(rollout_path):
        return None
    marker = f"Task name: {agent_path}\n" if agent_path else None
    try:
        with open(rollout_path, errors="replace") as f:
            for line in f:
                if '"response_item"' not in line:
                    continue
                try:
                    p = json.loads(line).get("payload") or {}
                except ValueError:
                    continue
                if p.get("type") == "agent_message":
                    text = "".join(x.get("text", "") for x in p.get("content") or [] if isinstance(x, dict))
                    m = NEW_TASK.search(text)
                    if m and (not agent_path or p.get("recipient") == agent_path or marker in text):
                        return m.group(1).strip()
                elif not agent_path and (p.get("type") in ("reasoning", "function_call", "custom_tool_call") or p.get("role") == "assistant"):
                    return ""
    except OSError:
        return None
    return None


class CodexSource:
    def __init__(self, state_path=None, history_path=None):
        self.state_path = state_path or config.codex_state_db()
        self.history_path = history_path or config.codex_history_db()

    def check(self):
        problems = []
        for label, path, required in (("state", self.state_path, STATE_REQUIRED),
                                      ("history", self.history_path, HISTORY_REQUIRED)):
            if not path or not path.exists():
                problems.append(f"{label} database not found under {config.CODEX_HOME}")
                continue
            try:
                with _open(path) as c:
                    for table, cols in required.items():
                        missing = cols - _columns(c, table)
                        if missing:
                            problems.append(f"{path.name}:{table} missing {sorted(missing)}")
            except sqlite3.Error as e:
                problems.append(f"{path.name}: {e}")
        return (not problems), problems

    def threads_since(self, watermark_ms):
        with _open(self.state_path) as c:
            rows = c.execute(
                """select t.*, coalesce(t.updated_at_ms, t.updated_at*1000) as upd_ms,
                          coalesce(t.created_at_ms, t.created_at*1000) as crt_ms,
                          e.parent_thread_id as edge_parent, e.status as edge_status
                   from threads t left join thread_spawn_edges e on e.child_thread_id = t.id
                   where coalesce(t.updated_at_ms, t.updated_at*1000) > ?
                   order by crt_ms""", (watermark_ms,)).fetchall()
            return [dict(r) for r in rows]

    def threads_by_ids(self, ids):
        if not ids:
            return []
        with _open(self.state_path) as c:
            marks = ",".join("?" * len(ids))
            rows = c.execute(
                f"""select t.*, coalesce(t.updated_at_ms, t.updated_at*1000) as upd_ms,
                          coalesce(t.created_at_ms, t.created_at*1000) as crt_ms,
                          e.parent_thread_id as edge_parent, e.status as edge_status
                   from threads t left join thread_spawn_edges e on e.child_thread_id = t.id
                   where t.id in ({marks})""", list(ids)).fetchall()
            return [dict(r) for r in rows]

    def archived_ids(self):
        with _open(self.state_path) as c:
            return {r[0] for r in c.execute("select id from threads where archived=1")}

    def projects(self):
        """Codex desktop's project list as [{"name", "path"}]; empty when this Codex build has no projects tables."""
        with _open(self.state_path) as c:
            if not {"projects", "project_roots"} <= {r[0] for r in c.execute("select name from sqlite_master where type='table'")}:
                return []
            return [{"name": r[0], "path": r[1]} for r in c.execute(
                "select p.name, r.path from projects p join project_roots r on r.project_id = p.id order by p.position, r.position")]

    def turns_for(self, thread_id):
        with _open(self.history_path) as c:
            return [dict(r) for r in c.execute(
                "select * from thread_turns where thread_id=? order by rollout_ordinal", (thread_id,))]

    def items_for_turn(self, thread_id, turn_id):
        with _open(self.history_path) as c:
            return [dict(r) for r in c.execute(
                "select item_id, item_type, item_json, created_at_ms from thread_items where thread_id=? and turn_id=? order by rollout_ordinal",
                (thread_id, turn_id))]

    def longest_pause_ms(self):
        """Longest silence between two items inside any turn that went on to complete (measured from this machine's history)."""
        best, prev = 0, None
        with _open(self.history_path) as c:
            for key, ms in c.execute("""select i.thread_id || '/' || i.turn_id, i.created_at_ms from thread_items i
                                        join thread_turns t on t.thread_id = i.thread_id and t.turn_id = i.turn_id
                                        where t.status = 'completed' and i.created_at_ms is not null
                                        order by i.thread_id, i.turn_id, i.rollout_ordinal"""):
                if prev and prev[0] == key:
                    best = max(best, ms - prev[1])
                prev = (key, ms)
        return best

    def user_messages(self, thread_id):
        """{turn_id: first user message text} for one thread."""
        out = {}
        with _open(self.history_path) as c:
            for turn_id, js in c.execute("""select turn_id, item_json from thread_items where thread_id=? and item_type='userMessage'
                                            order by rollout_ordinal""", (thread_id,)):
                if turn_id not in out:
                    try:
                        j = json.loads(js)
                    except ValueError:
                        continue
                    out[turn_id] = " ".join(x.get("text", "") for x in j.get("content", []) if x.get("type") == "text").strip()
        return out

    def last_agent_message(self, thread_id):
        """Text of the thread's latest agent message, or None. For a finished subagent this is its result."""
        with _open(self.history_path) as c:
            row = c.execute("""select item_json from thread_items where thread_id=? and item_type='agentMessage'
                               order by rollout_ordinal desc limit 1""", (thread_id,)).fetchone()
        if not row:
            return None
        try:
            return (json.loads(row[0]).get("text") or "").strip() or None
        except ValueError:
            return None

    def mcp_calls(self, thread_ids, server_like="%canvas%"):
        """MCP tool-call items for these threads whose server name matches server_like (SQL LIKE)."""
        if not thread_ids:
            return []
        out = []
        with _open(self.history_path) as c:
            for i in range(0, len(thread_ids), 500):
                chunk = thread_ids[i:i + 500]
                marks = ",".join("?" * len(chunk))
                out += [dict(r) for r in c.execute(
                    f"""select thread_id, turn_id, item_id, item_json, created_at_ms from thread_items
                        where thread_id in ({marks}) and item_type='mcpToolCall'
                        and lower(json_extract(item_json, '$.server')) like ?""",
                    (*chunk, server_like))]
        return out
