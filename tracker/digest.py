"""Short state digest injected by hooks after compaction/resume and at subagent start."""
from . import artifacts, config, store

STATE_LABEL = {"not_started": "not started", "in_progress": "in progress", "review": "review", "blocked": "blocked",
               "waiting": "waiting", "done": "done"}
TOOLS_LINE = ("Use the session_tracker MCP tools for details and updates: get_session, get_tree, search, add_todo, "
              "update_todo, record (decision | mistake | issue), update_ledger.")
# Agent-facing rules, re-sent with every digest (start, after compaction, subagent start) because the skill's own text
# does not survive compaction. Terse on purpose.
RULES = ("[session-tracker rules] Tracker = this session's durable state: todos; subagents (task/status/result); ledger: decisions, "
         "issues (triage log: symptom, cause/hypothesis, repro, fix), mistakes+lessons; artifacts. A scribe updates it after every turn "
         "(lags the current turn). Check it, don't trust memory: status/progress/done-vs-left -> get_session todos+children; "
         "error/debugging/retry -> search issues+mistakes first, follow lessons; what was decided/design from earlier discussion -> "
         "decisions (skip superseded); earlier report/doc/file -> artifacts; continuing earlier work -> search/get_tree, read that "
         "session. Write only if it can't wait for the scribe or the user asks: add_todo new work, update_todo state+note, "
         "delete_todo when dropped, record a decision whose wording matters or a mistake you notice. Save deliverables in the "
         "artifacts folder (files elsewhere may not be linked). Tools: get_session, get_tree, "
         "search, add_todo, update_todo, delete_todo, record, update_ledger, add_artifact. Details: session-tracker skill.")
TEMP_ROOTS = ("/tmp/", "/private/tmp/", "/var/folders/", "/private/var/folders/")


def _one_line(s, n=160):
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[:n - 1] + "…"


def _lesson_line(m):
    lesson = m.get("effective_lesson") or m.get("lesson")
    if lesson:
        mark = " [corrected]" if m.get("corrections") else ""
        return f"- {_one_line(lesson, 220)}{mark} (from: {_one_line(m['title'], 90)})"
    tail = f" Instead: {_one_line(m['what_worked'], 100)}" if m.get("what_worked") else ""
    return f"- {_one_line(m['title'])} → {_one_line(m['body'], 120)}{tail}"


ACTIVE = ("running", "stalled")
FINISHED = ("done", "failed", "interrupted", "archived")


def _child_line(ch):
    parts = [f"- {'[pinned] ' if ch.get('pinned_at') else ''}{_one_line(ch['display_title'], 120)} — {ch['status']}"]
    if ch.get("task"):
        parts.append(f"task: {_one_line(ch['task'])}")
    if ch.get("result") and ch["status"] in FINISHED:
        parts.append(f"result: {_one_line(ch['result'])}")
    n = ch["counts"]
    if n.get("todos_open") or n.get("issues_open"):
        parts.append(f"open todos {n.get('todos_open', 0)}, open issues {n.get('issues_open', 0)}")
    return " — ".join(parts) + f" (session {ch['id']})"


def _section(title, lines, budget):
    if not lines:
        return []
    out, used = [title], len(title)
    for i, line in enumerate(lines):
        if used + len(line) + 1 > budget:
            out.append(f"- (+{len(lines) - i} more via MCP)")
            break
        out.append(line)
        used += len(line) + 1
    return out


def build(c, session_id, event="SessionStart", source=None, parent_id=None, reserve=0):
    """reserve: characters the caller adds in front of the digest, so the whole hook output stays within the limit."""
    limit = config.DIGEST_MAX_CHARS - reserve
    try:
        view = store.session_view(c, session_id, rescan=False)
    except KeyError:
        view = None
    if event == "SubagentStart":
        return _subagent(c, session_id, parent_id, view, limit)
    if view is None:
        return _with_rules(f"[session-tracker] This session is tracked as {session_id}. "
                           f"Artifacts folder: {artifacts.default_dir(session_id, None)}.", limit)
    s = view["session"]
    head = [f"[session-tracker] Durable state for this session ({session_id}).",
            f"Title: {_one_line(s['display_title'], 200)}",
            f"Status: {s['status']} · artifacts folder: {s.get('artifacts_dir')}"]
    if source == "startup" and not (view["todos"] or any(view["ledger"].values()) or view["children"]):
        return _with_rules("\n".join(head), limit)
    open_todos = [t for t in view["todos"] if t["state"] != "done"]
    todo_lines = [f"- [{t['priority']} · {STATE_LABEL[t['state']]}] {_one_line(t['title'])} (todo #{t['id']})" for t in open_todos]
    decisions = [f"- [{d['status']}] {_one_line(d['title'])}" + (f" — {_one_line(d['rationale'], 120)}" if d.get("rationale") else "")
                 for d in view["ledger"]["decision"] if d["status"] != "superseded"]
    mistakes = [_lesson_line(m) for m in view["ledger"]["mistake"]]
    issues = [f"- {_one_line(i['title'])} (issue #{i['id']})" for i in view["ledger"]["issue"] if i["status"] == "open"]
    kids = sorted(view["children"], key=lambda ch: (ch.get("pinned_at") is None, ch["status"] not in ACTIVE, -(ch.get("created_at") or 0)))
    children = [_child_line(ch) for ch in kids]
    remaining = limit - sum(len(x) + 1 for x in head) - len(RULES) - 40
    peer_issues, peer_lessons = _project_memory(c, s) if not s.get("parent_id") else ([], [])
    sections = [("Open todos:", todo_lines), ("Child sessions (pinned, then running, then newest):", children), ("Open issues:", issues),
                ("Lessons from mistakes (do not repeat):", mistakes), ("Decisions (newest first):", decisions),
                ("Open issues from earlier sessions in this project:", peer_issues),
                ("Lessons from earlier sessions in this project:", peer_lessons)]
    body = []
    active = [sct for sct in sections if sct[1]]
    for idx, (title, lines) in enumerate(active):
        share = remaining // max(1, len(active) - idx)
        part = _section(title, lines, share)
        remaining -= sum(len(x) + 1 for x in part)
        body += part
    return _with_rules("\n".join(head + body), limit)


def _with_rules(text, limit):
    """State first, trimmed at a line break if needed, so the rules always arrive whole."""
    cap = max(0, limit - len(RULES) - 1)
    if len(text) > cap:
        text = text[:cap].rsplit("\n", 1)[0]
    return text + "\n" + RULES


def _project_memory(c, s):
    """Open issues and lessons recorded by other sessions on the same repository (or the same non-temporary folder),
    newest first, so a new session starts from what earlier sessions learned."""
    origin, cwd, root = s.get("git_origin_url"), s.get("cwd"), s.get("root_id") or s["id"]
    if origin:
        where, arg = "git_origin_url=?", origin
    elif cwd and not cwd.startswith(TEMP_ROOTS):
        where, arg = "cwd=?", cwd
    else:
        return [], []
    peers = [r[0] for r in c.execute(f"select id from sessions where {where} and coalesce(root_id, id) != ?", (arg, root))]
    if not peers:
        return [], []
    marks = ",".join("?" * len(peers))
    issues = c.execute(f"""select id, title, session_id from ledger where kind='issue' and status='open' and session_id in ({marks})
                           order by created_at desc""", peers).fetchall()
    mistakes = store._decorate(c, [dict(r) for r in c.execute(f"""select * from ledger where kind='mistake' and session_id in ({marks})
                                                                   order by created_at desc""", peers)])
    seen, lessons = set(), []
    for m in mistakes:
        line = _lesson_line(m)
        if line not in seen:
            seen.add(line)
            lessons.append(line)
    return [f"- {_one_line(i['title'])} (issue #{i['id']}, session {i['session_id']})" for i in issues], lessons


def _subagent(c, child_id, parent_id, view, limit):
    lines = [f"[session-tracker] You are subagent session {child_id}" + (f", spawned by session {parent_id}." if parent_id else "."),
             "A scribe records your decisions, issues, mistakes, todos and artifacts after each turn; "
             "write to the tracker yourself only as the rules below say."]
    if view:
        lines.append(f"Your artifacts folder: {view['session'].get('artifacts_dir')}")
    if parent_id:
        try:
            pv = store.session_view(c, parent_id, rescan=False)
        except KeyError:
            pv = None
        if pv:
            lines.append(f"Parent: {_one_line(pv['session']['display_title'], 160)}")
            mistakes = [_lesson_line(m) for m in pv["ledger"]["mistake"]]
            decisions = [f"- [{d['status']}] {_one_line(d['title'])}" for d in pv["ledger"]["decision"] if d["status"] == "decided"]
            budget = (limit - sum(len(x) for x in lines) - len(RULES)) // 2
            lines += _section("Lessons from the parent's mistakes (do not repeat):", mistakes, budget)
            lines += _section("Parent's decisions:", decisions, budget)
    return _with_rules("\n".join(lines), limit)

