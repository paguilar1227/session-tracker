"""Derive the session tree, statuses, turns, rule-based issues and canvas links from Codex records."""
import json
import logging
import sqlite3

from . import artifacts, codex_source, config, db, projects, restore
from .codex_source import TERMINAL, CodexSource

log = logging.getLogger("tracker.reconcile")


TASK_FINAL = ("done", "failed", "interrupted", "archived")


def derive_status(archived, last_turn_status, is_child):
    if archived:
        return "archived"
    if not last_turn_status:
        return "new"
    return {"inProgress": "running", "completed": "done" if is_child else "waiting", "failed": "failed",
            "interrupted": "interrupted"}.get(last_turn_status, last_turn_status)


def recompute_tree(c):
    rows = c.execute("select id, parent_id, root_id, depth from sessions").fetchall()
    parent = {r["id"]: r["parent_id"] for r in rows}
    current = {r["id"]: (r["root_id"], r["depth"]) for r in rows}
    changed = 0
    for sid in parent:
        root, depth, cur, seen = sid, 0, sid, {sid}
        while parent.get(cur) and parent[cur] in parent and parent[cur] not in seen:
            cur = parent[cur]
            seen.add(cur)
            root, depth = cur, depth + 1
        if current[sid] != (root, depth):
            c.execute("update sessions set root_id=?, depth=? where id=?", (root, depth, sid))
            changed += 1
    return changed


def _error_message(error_json):
    try:
        e = json.loads(error_json)
        return str(e.get("message") or error_json)
    except (ValueError, AttributeError, TypeError):
        return str(error_json)


def _canvas_ids(item_json):
    """Document ids a workflow-canvas tool call targeted: explicit argument or top-level result documentId."""
    try:
        j = json.loads(item_json)
    except ValueError:
        return None, []
    if not config.WORKFLOW_CANVAS_SERVER.search(str(j.get("server") or "")):
        return None, []
    ids, title = [], None
    args = j.get("arguments") or {}
    if isinstance(args, dict) and isinstance(args.get("documentId"), str):
        ids.append(args["documentId"])
    res = j.get("result") or {}
    for part in (res.get("content") or []) if isinstance(res, dict) else []:
        if part.get("type") != "text":
            continue
        try:
            body = json.loads(part.get("text") or "")
        except ValueError:
            continue
        if isinstance(body, dict) and isinstance(body.get("documentId"), str):
            ids.append(body["documentId"])
            title = body.get("title") if isinstance(body.get("title"), str) else title
    return title, list(dict.fromkeys(ids))


class Reconciler:
    def __init__(self, source=None):
        self.src = source or CodexSource()
        self.resynced = False
        self.stall_after_ms = None

    def mark_stalled(self, c):
        """Codex can leave a turn 'inProgress' forever (app quit, crash). A running session silent for longer than the longest
        pause ever seen inside a turn that completed is labelled 'stalled'; any new activity makes it 'running' again."""
        if self.stall_after_ms is None:
            try:
                self.stall_after_ms = self.src.longest_pause_ms() or 0
            except Exception as e:  # history unavailable: never mark anything stalled
                log.warning("could not measure turn pauses: %s", e)
                self.stall_after_ms = 0
            with db.write(c):
                db.set_meta("stall_after_ms", self.stall_after_ms, c)
        if not self.stall_after_ms:
            return 0
        with db.write(c):
            return c.execute("update sessions set status='stalled' where status='running' and archived=0 and coalesce(updated_at, 0) < ?",
                             (db.now_ms() - self.stall_after_ms,)).rowcount

    def run_once(self, c=None):
        c = c or db.conn()
        ok, problems = self.src.check()
        if (db.get_meta("codex_ok", None, c) != str(ok)) or (db.get_meta("codex_problems", "[]", c) != json.dumps(problems)):
            with db.write(c):
                db.set_meta("codex_ok", ok, c)
                db.set_meta("codex_problems", json.dumps(problems), c)
        if not ok:
            return {"ok": False, "problems": problems}
        # Full resync once per process start; afterwards only changed threads plus those still running
        # (a turn can finish in the history DB without the thread row changing again).
        wm = int(db.get_meta("threads_watermark", "0", c)) if self.resynced else 0
        threads = self.src.threads_since(wm)
        seen = {t["id"] for t in threads}
        running = [r[0] for r in c.execute("select id from sessions where status='running' and archived=0") if r[0] not in seen]
        rechecked = [t for t in self.src.threads_by_ids(running) if self._turns_changed(c, t["id"])]
        threads += rechecked
        # Archiving or unarchiving in Codex does not change a thread's updated_at, so compare the archived flags directly.
        have = seen | {t["id"] for t in rechecked}
        flipped = self.src.archived_ids() ^ {r[0] for r in c.execute("select id from sessions where archived=1")}
        threads += self.src.threads_by_ids(sorted(i for i in flipped if i not in have))
        self.resynced = True
        restore.prune(c)
        if not threads:
            self.mark_stalled(c)
            return {"ok": True, "threads": 0}
        extract_since = int(db.get_meta("extract_since", "0", c) or 0)
        index = artifacts.folder_index()
        ids = [t["id"] for t in threads]
        try:
            codex_projects = projects.with_remotes(self.src.projects())
        except sqlite3.Error as e:
            log.warning("could not read Codex projects: %s", e)
            codex_projects = None
        with db.write(c):
            if codex_projects is not None:
                db.set_meta("codex_projects", json.dumps(codex_projects), c)
            for t in threads:
                self._upsert_session(c, t, index, extract_since)
            recompute_tree(c)
            self._link_canvases(c, ids)
            db.set_meta("threads_watermark", max([wm] + [t["upd_ms"] or 0 for t in threads]), c)
            db.set_meta("last_reconcile_at", db.now_ms(), c)
        for sid in ids:
            try:
                with db.write(c):
                    artifacts.scan(c, sid)
            except Exception as e:  # a bad folder must not stop the cycle
                log.warning("artifact scan failed for %s: %s", sid, e)
        self.mark_stalled(c)
        return {"ok": True, "threads": len(threads)}

    def _turns_changed(self, c, sid):
        """Cheap check for a still-running session: did Codex's turn list for it change since we stored it?"""
        theirs = [(tr["turn_id"], tr["status"], tr["completed_at"]) for tr in self.src.turns_for(sid)]
        ours = [(r[0], r[1], (r[2] // 1000) if r[2] else None) for r in
                c.execute("select turn_id, status, completed_at from turns where session_id=? order by ordinal", (sid,))]
        return theirs != ours

    def _upsert_session(self, c, t, index, extract_since):
        sid = t["id"]
        is_child = bool(t.get("edge_parent"))
        turns = self.src.turns_for(sid)
        last = turns[-1]["status"] if turns else None
        status = derive_status(t["archived"], last, is_child)
        last_turn_at = max([(tr["completed_at"] or tr["started_at"] or 0) * 1000 for tr in turns] or [None]) if turns else None
        c.execute("""insert into sessions(id,parent_id,agent_path,nickname,role,title,first_message,cwd,model,effort,source,
                         edge_status,status,archived,created_at,updated_at,last_turn_at,turn_count,git_branch,rollout_path,git_origin_url)
                     values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                     on conflict(id) do update set parent_id=excluded.parent_id, agent_path=excluded.agent_path,
                         nickname=excluded.nickname, role=excluded.role, title=excluded.title, first_message=excluded.first_message,
                         cwd=excluded.cwd, model=excluded.model, effort=excluded.effort, source=excluded.source,
                         edge_status=excluded.edge_status, status=excluded.status, archived=excluded.archived,
                         created_at=excluded.created_at, updated_at=excluded.updated_at, last_turn_at=excluded.last_turn_at,
                         turn_count=excluded.turn_count, git_branch=excluded.git_branch, rollout_path=excluded.rollout_path,
                         git_origin_url=excluded.git_origin_url""",
                  (sid, t.get("edge_parent"), t.get("agent_path"), t.get("agent_nickname"), t.get("agent_role"),
                   t.get("title"), t.get("first_user_message"), t.get("cwd"), t.get("model"), t.get("reasoning_effort"),
                   t.get("thread_source") or ("subagent" if is_child else "user"), t.get("edge_status"), status,
                   int(t["archived"] or 0), t["crt_ms"], t["upd_ms"], last_turn_at, len(turns), t.get("git_branch"),
                   t.get("rollout_path"), t.get("git_origin_url") or None))
        s = dict(c.execute("select * from sessions where id=?", (sid,)).fetchone())
        if is_child:
            task = s["task"] if s["task"] is not None else codex_source.spawn_task(t.get("rollout_path"), t.get("agent_path"))
            if task is None and status in TASK_FINAL:
                task = ""
            c.execute("update sessions set task=?, result=? where id=?", (task, self.src.last_agent_message(sid), sid))
        artifacts.resolve_dir(c, s, index)
        db.index_doc(c, "session", sid, sid, " ".join(x for x in (t.get("agent_nickname"), t.get("agent_path"), t.get("title")) if x),
                     t.get("first_user_message") or "")
        for tr in turns:
            self._upsert_turn(c, sid, tr, extract_since)
        done = [tr["rollout_ordinal"] for tr in turns if tr["status"] == "completed"]
        if done:
            # A failed turn followed by a completed one: the automatic "Turn failed" issue is no longer open.
            c.execute("""update ledger set status='resolved', updated_at=? where session_id=? and source='rule' and kind='issue'
                         and status='open' and turn_id in (select turn_id from turns where session_id=? and ordinal < ?)""",
                      (db.now_ms(), sid, sid, max(done)))

    def _upsert_turn(self, c, sid, tr, extract_since):
        prev = c.execute("select status, extract_status from turns where session_id=? and turn_id=?", (sid, tr["turn_id"])).fetchone()
        completed_ms = tr["completed_at"] * 1000 if tr["completed_at"] else None
        extract = prev["extract_status"] if prev else "skipped"
        became_terminal = tr["status"] in TERMINAL and (prev is None or prev["status"] != tr["status"])
        if (became_terminal and config.EXTRACT_ENABLED and extract == "skipped" and extract_since
                and completed_ms and completed_ms >= extract_since):
            extract = "pending"
        error = _error_message(tr["error_json"]) if tr["error_json"] else None
        c.execute("""insert into turns(session_id,turn_id,ordinal,status,started_at,completed_at,duration_ms,error,extract_status)
                     values(?,?,?,?,?,?,?,?,?) on conflict(session_id,turn_id) do update set ordinal=excluded.ordinal,
                     status=excluded.status, started_at=excluded.started_at, completed_at=excluded.completed_at,
                     duration_ms=excluded.duration_ms, error=excluded.error, extract_status=excluded.extract_status""",
                  (sid, tr["turn_id"], tr["rollout_ordinal"], tr["status"], (tr["started_at"] or 0) * 1000 or None,
                   completed_ms, tr["duration_ms"], error, extract))
        if tr["status"] == "failed" and error:
            exists = c.execute("select 1 from ledger where session_id=? and turn_id=? and source='rule'", (sid, tr["turn_id"])).fetchone()
            if not exists:
                from . import store
                store.create_ledger(c, sid, {"kind": "issue", "title": f"Turn failed: {error[:140]}", "body": error,
                                             "status": "open", "made_by": "codex", "turn_id": tr["turn_id"]}, source="rule")

    def _link_canvases(self, c, ids):
        """Idempotent: every workflow-canvas call in the changed threads links its target document."""
        for item in self.src.mcp_calls(ids):
            title, doc_ids = _canvas_ids(item["item_json"])
            for doc_id in doc_ids:
                artifacts.link_canvas(c, item["thread_id"], doc_id, title)
