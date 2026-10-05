"""Domain operations shared by the HTTP API (and, through it, the MCP server)."""
import json
import re

from . import artifacts, db, projects
from .db import LEDGER_KINDS, LEDGER_STATUS, PRIORITIES, TODO_STATES

SESSION_LIST_COLS = "id,parent_id,root_id,depth,agent_path,nickname,role,title,first_message,status,archived,created_at,updated_at,last_turn_at,turn_count,model,cwd,git_origin_url,pinned_at"
TODO_ORDER = "case state when 'done' then 1 else 0 end, priority, position, id"


def display_title(s):
    title = (s.get("title") or s.get("first_message") or "").strip().splitlines()[0:1]
    title = title[0] if title else ""
    if s.get("nickname") or s.get("agent_path"):
        label = s.get("nickname") or ""
        path = s.get("agent_path") or ""
        name = f"{label} · {path}" if label and path else (label or path)
        return name if not title else f"{name} — {title[:90]}"
    return title[:140] or s["id"]


def get_session(c, session_id):
    row = c.execute("select * from sessions where id=?", (session_id,)).fetchone()
    if not row:
        raise KeyError(session_id)
    s = dict(row)
    s["display_title"] = display_title(s)
    s["project"] = projects.label(s.get("cwd"), s.get("git_origin_url"), codex_projects(c))
    return s


def codex_projects(c):
    try:
        return json.loads(db.get_meta("codex_projects", "[]", c) or "[]")
    except ValueError:
        return []


def _counts(c, ids):
    if not ids:
        return {}
    out = {i: {"todos_open": 0, "todos_total": 0, "decisions": 0, "mistakes": 0, "issues_open": 0, "artifacts": 0, "children": 0} for i in ids}
    marks = ",".join("?" * len(ids))
    for r in c.execute(f"select session_id, count(*) n, sum(state!='done') open from todos where session_id in ({marks}) group by 1", ids):
        out[r[0]]["todos_total"], out[r[0]]["todos_open"] = r[1], r[2] or 0
    for r in c.execute(f"""select session_id, kind, count(*), sum(kind='issue' and status='open') from ledger
                           where session_id in ({marks}) group by 1,2""", ids):
        key = {"decision": "decisions", "mistake": "mistakes", "issue": "issues_open"}[r[1]]
        out[r[0]][key] = r[3] if r[1] == "issue" else r[2]
    for r in c.execute(f"select session_id, count(*) from artifacts where missing=0 and session_id in ({marks}) group by 1", ids):
        out[r[0]]["artifacts"] = r[1]
    for r in c.execute(f"select parent_id, count(*) from sessions where parent_id in ({marks}) group by 1", ids):
        out[r[0]]["children"] = r[1]
    return out


def tree(c, include_archived=False, root_id=None):
    """All sessions as nested nodes (roots by recency)."""
    where = "" if include_archived else "where archived=0"
    rows = [dict(r) for r in c.execute(f"select {SESSION_LIST_COLS} from sessions {where}")]
    if root_id:
        rows = [r for r in rows if r["root_id"] == root_id or r["id"] == root_id]
    counts = _counts(c, [r["id"] for r in rows])
    roots_list = codex_projects(c)
    by_id = {}
    for r in rows:
        node = {k: r[k] for k in ("id", "parent_id", "depth", "status", "archived", "updated_at", "created_at",
                                  "agent_path", "nickname", "turn_count", "last_turn_at", "pinned_at")}
        node["title"] = display_title(r)
        node["project"] = projects.label(r["cwd"], r["git_origin_url"], roots_list)
        node["counts"] = counts.get(r["id"], {})
        node["children"] = []
        by_id[r["id"]] = node
    roots = []
    for node in by_id.values():
        parent = by_id.get(node["parent_id"]) if node["parent_id"] else None
        (parent["children"] if parent else roots).append(node)

    def latest(n):
        n["activity_at"] = max([n["updated_at"] or 0] + [latest(ch) for ch in n["children"]])
        return n["activity_at"]

    def sort(nodes):
        nodes.sort(key=lambda n: (n["pinned_at"] is None, n["created_at"] or 0))
        for n in nodes:
            sort(n["children"])

    for r in roots:
        sort(r["children"])
        latest(r)
    roots.sort(key=lambda n: n["activity_at"], reverse=True)
    return roots


def is_in_subtree(c, session_id, ancestor_id):
    """True if session_id is ancestor_id or one of its descendants."""
    cur, seen = session_id, set()
    while cur and cur not in seen:
        if cur == ancestor_id:
            return True
        seen.add(cur)
        row = c.execute("select parent_id from sessions where id=?", (cur,)).fetchone()
        cur = row[0] if row else None
    return False


def breadcrumb(c, s):
    chain = []
    cur = s
    seen = set()
    while cur and cur.get("parent_id") and cur["parent_id"] not in seen:
        seen.add(cur["parent_id"])
        row = c.execute("select id,parent_id,title,first_message,nickname,agent_path from sessions where id=?", (cur["parent_id"],)).fetchone()
        if not row:
            break
        cur = dict(row)
        chain.append({"id": cur["id"], "title": display_title(cur)})
    return list(reversed(chain))


def todos_for(c, session_id):
    todos = db.rows(c.execute(f"select * from todos where session_id=? order by {TODO_ORDER}", (session_id,)))
    if todos:
        ids = [t["id"] for t in todos]
        marks = ",".join("?" * len(ids))
        att = {}
        for a in db.rows(c.execute(f"""select ta.*, a.path as artifact_path, a.kind as artifact_kind, a.title as artifact_title,
                                       a.canvas_id as artifact_canvas_id from todo_attachments ta
                                       left join artifacts a on a.id = ta.artifact_id where ta.todo_id in ({marks}) order by ta.id""", ids)):
            att.setdefault(a["todo_id"], []).append(a)
        for t in todos:
            t["attachments"] = att.get(t["id"], [])
    return todos


def _decorate(c, entries):
    """Attach corrections (mistakes) and 'wrong' feedback; effective_* is what readers should act on."""
    ids = [e["id"] for e in entries]
    corr, wrong = {}, {}
    if ids:
        marks = ",".join("?" * len(ids))
        for r in db.rows(c.execute(f"select * from corrections where ledger_id in ({marks}) order by id", ids)):
            r["evidence"] = json.loads(r["evidence"]) if r.get("evidence") else []
            corr.setdefault(r["ledger_id"], []).append(r)
        for r in c.execute(f"select ledger_id, count(*) from feedback where kind='wrong' and ledger_id in ({marks}) group by ledger_id", ids):
            wrong[r[0]] = r[1]
    for e in entries:
        e["flagged_wrong"] = wrong.get(e["id"], 0)
        if e["kind"] != "mistake":
            continue
        e["corrections"] = corr.get(e["id"], [])
        e["effective_lesson"] = next((x["lesson"] for x in reversed(e["corrections"]) if x.get("lesson")), e.get("lesson"))
        e["effective_cause"] = next((x["cause"] for x in reversed(e["corrections"]) if x.get("cause")), e.get("rationale"))
    return entries


def ledger_for(c, session_id):
    out = {k: [] for k in LEDGER_KINDS}
    entries = db.rows(c.execute("select * from ledger where session_id=? order by created_at desc", (session_id,)))
    for r in entries:
        for key in ("alternatives", "evidence"):
            try:
                r[key] = json.loads(r[key]) if r[key] else []
            except ValueError:
                r[key] = []
    for r in _decorate(c, entries):
        out[r["kind"]].append(r)
    return out


def _fill_canvas_titles(c, session_id):
    """Canvases linked from tool calls often carry only an id; fetch titles from Workflow Canvas when it is up."""
    missing = c.execute("select id, canvas_id from artifacts where session_id=? and kind='canvas' and title is null", (session_id,)).fetchall()
    if not missing:
        return
    from . import canvas
    try:
        titles = {d["id"]: d.get("title") for d in canvas.list_documents()}
    except canvas.CanvasError:
        return
    with db.write(c):
        for a in missing:
            if titles.get(a["canvas_id"]):
                c.execute("update artifacts set title=? where id=?", (titles[a["canvas_id"]], a["id"]))
                db.index_doc(c, "artifact", a["id"], session_id, titles[a["canvas_id"]], a["canvas_id"])


def session_view(c, session_id, rescan=True):
    s = get_session(c, session_id)
    if rescan:
        with db.write(c):
            artifacts.scan(c, session_id)
        _fill_canvas_titles(c, session_id)
        s = get_session(c, session_id)
    children = [dict(r) for r in c.execute(f"select {SESSION_LIST_COLS}, task, result from sessions where parent_id=? order by pinned_at is null, created_at", (session_id,))]
    counts = _counts(c, [ch["id"] for ch in children] + [session_id])
    for ch in children:
        ch["display_title"] = display_title(ch)
        ch["counts"] = counts.get(ch["id"], {})
    turns = db.rows(c.execute("select turn_id,status,started_at,completed_at,extract_status,extract_error from turns where session_id=? order by ordinal", (session_id,)))
    return {
        "session": s,
        "counts": counts.get(session_id, {}),
        "breadcrumb": breadcrumb(c, s),
        "todos": todos_for(c, session_id),
        "artifacts": db.rows(c.execute("select * from artifacts where session_id=? and missing=0 order by kind, title", (session_id,))),
        "ledger": ledger_for(c, session_id),
        "children": children,
        "turns": {"total": len(turns),
                  "pending_extraction": sum(t["extract_status"] in ("pending", "running") for t in turns),
                  "failed_extraction": [t for t in turns if t["extract_status"] == "failed"][-3:]},
    }


def _clean(v):
    return v.strip() if isinstance(v, str) else v


def _check(value, allowed, field):
    if value not in allowed:
        raise ValueError(f"{field} must be one of {', '.join(allowed)}")
    return value


def create_todo(c, session_id, data, created_by="user"):
    get_session(c, session_id)
    title = _clean(data.get("title") or "")
    if not title:
        raise ValueError("title is required")
    now = db.now_ms()
    position = c.execute("select coalesce(max(position), 0) + 1 from todos where session_id=?", (session_id,)).fetchone()[0]
    cur = c.execute("""insert into todos(session_id,title,notes,priority,state,created_at,updated_at,created_by,source_ref,position)
                       values(?,?,?,?,?,?,?,?,?,?)""",
                    (session_id, title, data.get("notes") or "", _check(data.get("priority") or "P2", PRIORITIES, "priority"),
                     _check(data.get("state") or "not_started", TODO_STATES, "state"), now, now, created_by, data.get("source_ref"),
                     position))
    db.index_doc(c, "todo", cur.lastrowid, session_id, title, data.get("notes") or "")
    db.log_event(c, created_by, "todo.created", session_id, {"todo_id": cur.lastrowid})
    return get_todo(c, cur.lastrowid)


def get_todo(c, todo_id):
    row = c.execute("select * from todos where id=?", (todo_id,)).fetchone()
    if not row:
        raise KeyError(f"todo {todo_id}")
    t = dict(row)
    t["attachments"] = db.rows(c.execute("select * from todo_attachments where todo_id=? order by id", (todo_id,)))
    return t


def update_todo(c, todo_id, data, actor="user"):
    t = get_todo(c, todo_id)
    fields = {}
    if "title" in data:
        fields["title"] = _clean(data["title"]) or t["title"]
    if "notes" in data:
        fields["notes"] = data["notes"] or ""
    if "priority" in data:
        fields["priority"] = _check(data["priority"], PRIORITIES, "priority")
    if "state" in data:
        fields["state"] = _check(data["state"], TODO_STATES, "state")
    if not fields:
        return t
    fields["updated_at"] = db.now_ms()
    c.execute(f"update todos set {', '.join(k + '=?' for k in fields)} where id=?", (*fields.values(), todo_id))
    t = get_todo(c, todo_id)
    db.index_doc(c, "todo", todo_id, t["session_id"], t["title"], t["notes"])
    db.log_event(c, actor, "todo.updated", t["session_id"], {"todo_id": todo_id, "fields": sorted(fields)})
    return t


def move_todo(c, todo_id, data, actor="user"):
    """Put a todo right before or after another todo of the same session, optionally with a new priority.

    Todos are shown by priority, then position, so a todo dragged into another priority's group takes that priority;
    positions are then renumbered in display order. Without an anchor the todo goes to the end of its priority."""
    t = get_todo(c, todo_id)
    where = "before" if data.get("before") is not None else "after" if data.get("after") is not None else None
    anchor = None
    if where:
        try:
            anchor = get_todo(c, int(data[where]))
        except (TypeError, ValueError):
            raise ValueError(f"{where} must be a todo id")
        if anchor["session_id"] != t["session_id"]:
            raise ValueError("todos can only be reordered within their own session")
        if anchor["id"] == t["id"]:
            raise ValueError("a todo cannot be placed relative to itself")
    if data.get("priority") and data["priority"] != t["priority"]:
        t = update_todo(c, todo_id, {"priority": data["priority"]}, actor=actor)
    ids = [r[0] for r in c.execute(f"select id from todos where session_id=? order by {TODO_ORDER}", (t["session_id"],))
           if r[0] != todo_id]
    if anchor:
        i = ids.index(anchor["id"])
        ids.insert(i if where == "before" else i + 1, todo_id)
    else:
        ids.append(todo_id)
    for pos, tid in enumerate(ids, 1):
        c.execute("update todos set position=? where id=?", (pos, tid))
    db.log_event(c, actor, "todo.moved", t["session_id"], {"todo_id": todo_id, where or "to": anchor["id"] if anchor else "end",
                                                          "priority": t["priority"]})
    return get_todo(c, todo_id)


def set_pinned(c, kind, ref, pinned, actor="user"):
    """Pin or unpin a session or an artifact; pinned ones are listed first. Re-pinning keeps the original time."""
    table = {"session": "sessions", "artifact": "artifacts"}[kind]
    row = c.execute(f"select id, pinned_at, {'id' if kind == 'session' else 'session_id'} as sid from {table} where id=?", (ref,)).fetchone()
    if not row:
        raise KeyError(f"{kind} {ref}")
    value = (row["pinned_at"] or db.now_ms()) if pinned else None
    c.execute(f"update {table} set pinned_at=? where id=?", (value, ref))
    db.log_event(c, actor, f"{kind}.{'pinned' if pinned else 'unpinned'}", row["sid"], {"id": ref})
    return {"id": ref, "pinned": bool(value), "pinned_at": value}


def delete_todo(c, todo_id, actor="user", reason=None):
    t = get_todo(c, todo_id)
    c.execute("delete from todos where id=?", (todo_id,))
    db.unindex_doc(c, "todo", todo_id)
    db.log_event(c, actor, "todo.deleted", t["session_id"], {"todo_id": todo_id, "title": t["title"], "notes": t["notes"],
                                                            "state": t["state"], "priority": t["priority"], "reason": reason})


def add_attachment(c, todo_id, data):
    t = get_todo(c, todo_id)
    kind = _check(data.get("kind"), ("url", "artifact", "reference"), "kind")
    url = artifact_id = None
    title = _clean(data.get("title"))
    if kind == "url":
        url = _clean(data.get("url") or "")
        if not re.match(r"^(https?|file|mailto):", url or "", re.I):
            raise ValueError("url must start with http(s)://, file:// or mailto:")
        title = title or url
    else:
        artifact_id = int(data.get("artifact_id") or 0)
        a = c.execute("select * from artifacts where id=?", (artifact_id,)).fetchone()
        if not a:
            raise ValueError("artifact_id not found")
        title = title or a["title"]
    cur = c.execute("insert into todo_attachments(todo_id,kind,url,artifact_id,title,created_at) values(?,?,?,?,?,?)",
                    (todo_id, kind, url, artifact_id, title, db.now_ms()))
    c.execute("update todos set updated_at=? where id=?", (db.now_ms(), todo_id))
    db.log_event(c, "user", "todo.attachment", t["session_id"], {"todo_id": todo_id, "kind": kind})
    return dict(c.execute("select * from todo_attachments where id=?", (cur.lastrowid,)).fetchone())


def delete_attachment(c, attachment_id):
    c.execute("delete from todo_attachments where id=?", (attachment_id,))


def create_ledger(c, session_id, data, source="agent"):
    get_session(c, session_id)
    kind = _check(data.get("kind"), LEDGER_KINDS, "kind")
    title = _clean(data.get("title") or "")
    if not title:
        raise ValueError("title is required")
    status = data.get("status") or LEDGER_STATUS[kind][0]
    _check(status, LEDGER_STATUS[kind], f"status for {kind}")
    now = db.now_ms()
    cur = c.execute("""insert into ledger(session_id,kind,title,body,status,made_by,rationale,alternatives,what_worked,lesson,evidence,
                       turn_id,source,model,created_at,updated_at) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (session_id, kind, title, data.get("body") or "", status, data.get("made_by"), data.get("rationale"),
                     json.dumps(_alternatives(data.get("alternatives"))), data.get("what_worked"), data.get("lesson"),
                     json.dumps(data.get("evidence") or []), data.get("turn_id"), source, data.get("model"), now, now))
    body = " ".join(str(x or "") for x in (data.get("body"), data.get("rationale"), data.get("what_worked"), data.get("lesson")))
    db.index_doc(c, kind, cur.lastrowid, session_id, title, body)
    return get_ledger(c, cur.lastrowid)


def get_ledger(c, entry_id):
    row = c.execute("select * from ledger where id=?", (entry_id,)).fetchone()
    if not row:
        raise KeyError(f"ledger {entry_id}")
    e = dict(row)
    for key in ("alternatives", "evidence"):
        try:
            e[key] = json.loads(e[key]) if e[key] else []
        except (TypeError, ValueError):
            e[key] = []
    return _decorate(c, [e])[0]


def _alternatives(v):
    if isinstance(v, str):
        v = re.split(r"\s*(?:\n|·|;)\s*", v)
    return [str(x).strip() for x in (v or []) if str(x).strip()]


def update_ledger(c, entry_id, data, actor="user"):
    e = get_ledger(c, entry_id)
    allowed = db.LEDGER_EDITABLE[e["kind"]]
    if not allowed:
        raise ValueError("Mistakes are permanent records and can't be edited. Record a new mistake if there is more to learn.")
    blocked = sorted(k for k in data if k not in allowed and k not in ("kind", "session_id"))
    if blocked:
        raise ValueError(f"{e['kind']} entries can't change: {', '.join(blocked)} (editable: {', '.join(allowed)})")
    fields = {}
    for key in ("title", "body", "rationale", "made_by"):
        if key in data:
            fields[key] = data[key]
    if "alternatives" in data:
        fields["alternatives"] = json.dumps(_alternatives(data["alternatives"]))
    if "status" in data:
        fields["status"] = _check(data["status"], LEDGER_STATUS[e["kind"]], f"status for {e['kind']}")
    if "title" in fields and not (fields["title"] or "").strip():
        raise ValueError("title can't be empty")
    if not fields:
        return e
    fields["updated_at"] = db.now_ms()
    c.execute(f"update ledger set {', '.join(k + '=?' for k in fields)} where id=?", (*fields.values(), entry_id))
    e = get_ledger(c, entry_id)
    db.index_doc(c, e["kind"], entry_id, e["session_id"], e["title"], " ".join(str(x or "") for x in (e["body"], e["rationale"], e["what_worked"], e["lesson"])))
    db.log_event(c, actor, "ledger.updated", e["session_id"], {"id": entry_id, "fields": sorted(fields)})
    return e


def delete_ledger(c, entry_id, actor="user"):
    if actor != "user":
        raise ValueError("Only you can delete ledger entries (from the UI).")
    e = get_ledger(c, entry_id)
    c.execute("delete from ledger where id=?", (entry_id,))
    db.unindex_doc(c, e["kind"], entry_id)


def _reindex_mistake(c, e):
    texts = [e["body"], e["rationale"], e["what_worked"], e["lesson"]]
    for x in e.get("corrections") or []:
        texts += [x.get("cause"), x.get("lesson"), x.get("note")]
    db.index_doc(c, e["kind"], e["id"], e["session_id"], e["title"], " ".join(str(t or "") for t in texts))


def create_correction(c, ledger_id, data, source="user", model=None):
    """Mistakes stay as written; a correction is appended on top and becomes the lesson readers see."""
    e = get_ledger(c, ledger_id)
    if e["kind"] != "mistake":
        raise ValueError("Corrections are for mistakes. Decisions and issues can be edited directly.")
    _check(source, db.CORRECTION_SOURCES, "source")
    cause, lesson, note = (_clean(data.get(k) or "") for k in ("cause", "lesson", "note"))
    if not (cause or lesson):
        raise ValueError("a correction needs a corrected cause or lesson")
    cur = c.execute("""insert into corrections(ledger_id,session_id,cause,lesson,note,source,turn_id,evidence,model,created_at)
                       values(?,?,?,?,?,?,?,?,?,?)""",
                    (ledger_id, e["session_id"], cause or None, lesson or None, note or None, source, data.get("turn_id"),
                     json.dumps(data.get("evidence") or []), model, db.now_ms()))
    c.execute("update ledger set updated_at=? where id=?", (db.now_ms(), ledger_id))
    _reindex_mistake(c, get_ledger(c, ledger_id))
    db.log_event(c, source, "ledger.corrected", e["session_id"], {"id": ledger_id, "correction_id": cur.lastrowid})
    return dict(c.execute("select * from corrections where id=?", (cur.lastrowid,)).fetchone())


def create_feedback(c, data):
    """A user label for evaluating the scribe: 'wrong' flags a ledger entry, 'missed' points at a turn with an unrecorded mistake."""
    kind = _check(data.get("kind"), db.FEEDBACK_KINDS, "kind")
    note = _clean(data.get("note") or "")
    if kind == "wrong":
        e = get_ledger(c, int(data.get("ledger_id") or 0))
        session_id, ledger_id, turn_id = e["session_id"], e["id"], e.get("turn_id")
    else:
        session_id = data.get("session_id")
        get_session(c, session_id)
        turn_id = data.get("turn_id")
        if not turn_id or not c.execute("select 1 from turns where session_id=? and turn_id=?", (session_id, turn_id)).fetchone():
            raise ValueError("turn_id must be a turn of this session")
        if not note:
            raise ValueError("describe the mistake that was missed")
        ledger_id = None
    cur = c.execute("insert into feedback(session_id,kind,ledger_id,turn_id,note,created_at) values(?,?,?,?,?,?)",
                    (session_id, kind, ledger_id, turn_id, note, db.now_ms()))
    db.log_event(c, "user", "feedback." + kind, session_id, {"feedback_id": cur.lastrowid, "ledger_id": ledger_id, "turn_id": turn_id})
    return dict(c.execute("select * from feedback where id=?", (cur.lastrowid,)).fetchone())


def list_feedback(c, session_id=None):
    sql = "select f.*, l.title as ledger_title, l.kind as ledger_kind from feedback f left join ledger l on l.id=f.ledger_id"
    args = []
    if session_id:
        sql += " where f.session_id=?"
        args.append(session_id)
    return db.rows(c.execute(sql + " order by f.id desc", args))


def delete_feedback(c, feedback_id):
    c.execute("delete from feedback where id=?", (feedback_id,))


def session_turns(c, session_id):
    """Finished turns with the first user message, for the 'missed a mistake' picker."""
    return db.rows(c.execute("""select turn_id, ordinal, status, completed_at, extract_status from turns
                                where session_id=? order by ordinal desc""", (session_id,)))


def fts_query(q):
    terms = re.findall(r"[\w\-]+", q or "", re.UNICODE)
    return " AND ".join('"' + t.replace('"', '') + '"*' for t in terms[:12])


def search(c, q, kinds=None, limit=50):
    query = fts_query(q)
    if not query:
        return []
    sql = """select kind, ref_id, session_id, title, snippet(search_fts, 4, '[', ']', ' … ', 12) as snippet, bm25(search_fts) as rank
             from search_fts where search_fts match ?"""
    args = [query]
    if kinds:
        sql += f" and kind in ({','.join('?' * len(kinds))})"
        args += list(kinds)
    sql += " order by rank limit ?"
    args.append(limit)
    out = db.rows(c.execute(sql, args))
    ids = list({r["session_id"] for r in out if r["session_id"]})
    titles = {}
    if ids:
        for r in c.execute(f"select id,title,first_message,nickname,agent_path from sessions where id in ({','.join('?' * len(ids))})", ids):
            titles[r["id"]] = display_title(dict(r))
    for r in out:
        r["session_title"] = titles.get(r["session_id"])
    return out

