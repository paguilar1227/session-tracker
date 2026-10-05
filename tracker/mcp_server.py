"""MCP server (stdio) for Codex sessions. Identifies the calling session from _meta.threadId."""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

from . import __version__, config
from .digest import ACTIVE, FINISHED

INSTRUCTIONS = ("Session Tracker keeps durable state for this Codex session and its subagents: todos, artifacts, and a "
                "ledger of decisions, mistakes and issues. Calls are attributed to the calling session automatically. "
                "Read the mistakes' lessons (get_session) before repeating work; they were written so you don't repeat them. "
                "When a mistake has corrections, effective_lesson is the one to follow.")

STATES = ["not_started", "in_progress", "review", "blocked", "waiting", "done"]
SECTIONS = ["todos", "children", "artifacts", "ledger", "extraction"]
SID = {"type": "string", "description": "Session (thread) id. Omit to use the calling session."}
TOOLS = [
    {"name": "get_session", "description": ("Full state of a session: status, todos, artifacts, ledger (decisions, mistakes, issues) and child sessions. "
                                            "Todos are in the user's priority order: open before done, then priority (P1 first), then the user's "
                                            "manual order, so an earlier todo matters more. Pinned children and artifacts are ones the user marked as important. "
                                            "Each child shows the task it was spawned with and, once finished, its result (one line; call get_session with "
                                            "the child's session_id for the full text); children are pinned first, then running, then newest. "
                                            "For status or progress, pass include [\"todos\", \"children\"] to get just those."),
     "inputSchema": {"type": "object", "properties": {"session_id": SID,
                                                      "include": {"type": "array", "items": {"type": "string", "enum": SECTIONS},
                                                                  "description": "Sections to return (default all). The session header is always included."}}}},
    {"name": "get_tree", "description": "The whole session tree (root, subagents, their subagents) containing a session, with statuses and counts.",
     "inputSchema": {"type": "object", "properties": {"session_id": SID}}},
    {"name": "search", "description": "Full-text search across sessions, todos, decisions, mistakes, issues and artifacts.",
     "inputSchema": {"type": "object", "properties": {"query": {"type": "string"},
                                                      "kinds": {"type": "array", "items": {"type": "string", "enum": ["session", "todo", "decision", "mistake", "issue", "artifact"]}}},
                     "required": ["query"]}},
    {"name": "add_todo", "description": "Add a todo. Notes support Markdown and emoji.",
     "inputSchema": {"type": "object", "properties": {"title": {"type": "string"}, "notes": {"type": "string"},
                                                      "priority": {"type": "string", "enum": ["P1", "P2", "P3"]},
                                                      "state": {"type": "string", "enum": STATES}, "session_id": SID},
                     "required": ["title"]}},
    {"name": "update_todo", "description": "Update a todo's title, notes, priority or state.",
     "inputSchema": {"type": "object", "properties": {"todo_id": {"type": "integer"}, "title": {"type": "string"}, "notes": {"type": "string"},
                                                      "priority": {"type": "string", "enum": ["P1", "P2", "P3"]},
                                                      "state": {"type": "string", "enum": STATES}},
                     "required": ["todo_id"]}},
    {"name": "delete_todo", "description": "Delete a todo permanently (with its attachments). Works for todos of your own session or its subagents. To close one out instead, set its state to done with update_todo.",
     "inputSchema": {"type": "object", "properties": {"todo_id": {"type": "integer"}}, "required": ["todo_id"]}},
    {"name": "attach_to_todo", "description": "Attach a URL or a file (artifact) to a todo.",
     "inputSchema": {"type": "object", "properties": {"todo_id": {"type": "integer"}, "url": {"type": "string"},
                                                      "artifact_path": {"type": "string", "description": "Absolute path of a file to attach as an artifact link."},
                                                      "title": {"type": "string"}},
                     "required": ["todo_id"]}},
    {"name": "record", "description": ("Record a ledger entry. decision: title, body, rationale, alternatives, status proposed|decided|superseded. "
                                       "issue: title, body, status open|resolved. mistake (permanent once written): title, body = what happened, "
                                       "rationale = why it was a mistake, lesson = a direct instruction that stops a future session repeating it."),
     "inputSchema": {"type": "object", "properties": {"kind": {"type": "string", "enum": ["decision", "mistake", "issue"]},
                                                      "title": {"type": "string"}, "body": {"type": "string"},
                                                      "status": {"type": "string"}, "rationale": {"type": "string"},
                                                      "alternatives": {"type": "array", "items": {"type": "string"}},
                                                      "lesson": {"type": "string"}, "session_id": SID},
                     "required": ["kind", "title"]}},
    {"name": "update_ledger", "description": ("Edit a decision (title, body, rationale, alternatives, status) or an issue (title, body, status). "
                                              "Mistakes are permanent and can't be edited; only the user (UI) or the scribe can append a correction."),
     "inputSchema": {"type": "object", "properties": {"entry_id": {"type": "integer"}, "status": {"type": "string"},
                                                      "title": {"type": "string"}, "body": {"type": "string"},
                                                      "rationale": {"type": "string"},
                                                      "alternatives": {"type": "array", "items": {"type": "string"}}},
                     "required": ["entry_id"]}},
    {"name": "add_artifact", "description": "Link a file outside the session's artifacts folder so it shows as an artifact. Files saved inside the folder appear automatically.",
     "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}, "title": {"type": "string"}, "session_id": SID},
                     "required": ["path"]}},
    {"name": "link_canvas", "description": "Link a Workflow Canvas document to the session (canvases you edit through the workflow-canvas MCP are linked automatically).",
     "inputSchema": {"type": "object", "properties": {"document_id": {"type": "string"}, "title": {"type": "string"}, "session_id": SID},
                     "required": ["document_id"]}},
    {"name": "get_digest", "description": "The short state digest that is injected after compaction.",
     "inputSchema": {"type": "object", "properties": {"session_id": SID}}},
    {"name": "extract_now", "description": "Queue this session's finished turns for ledger extraction (normally automatic).",
     "inputSchema": {"type": "object", "properties": {"session_id": SID}}},
]


class ToolError(Exception):
    pass


def api(method, path, body=None, caller=None):
    headers = {"Content-Type": "application/json", "X-Actor": "agent"}
    if caller:
        headers["X-Caller-Session"] = caller
    req = urllib.request.Request(config.BASE_URL + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            msg = json.load(e).get("error")
        except ValueError:
            msg = e.reason
        raise ToolError(f"{e.code}: {msg}")
    except (urllib.error.URLError, OSError) as e:
        raise ToolError(f"session-tracker service is not reachable at {config.BASE_URL} ({e}). Restart it with: "
                        f"launchctl kickstart -k gui/{os.getuid()}/{config.LABEL} (or re-run {config.PROJECT_DIR / 'install.sh'})")


def calling_session(meta):
    meta = meta or {}
    return meta.get("threadId") or (meta.get("x-codex-turn-metadata") or {}).get("thread_id")


def _one_line(text, n=160):
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[:n - 1] + "…"


def _compact_view(v, include=None):
    s = v["session"]
    kids = sorted(v["children"], key=lambda ch: (ch.get("pinned_at") is None, ch["status"] not in ACTIVE, -(ch.get("created_at") or 0)))
    view = {
        "session": {k: s.get(k) for k in ("id", "display_title", "status", "parent_id", "root_id", "depth", "agent_path",
                                          "nickname", "model", "cwd", "artifacts_dir", "created_at", "updated_at", "turn_count",
                                          "task", "result") if k not in ("task", "result") or (s.get(k) and (k == "task" or s.get("status") in FINISHED))},
        "breadcrumb": v["breadcrumb"],
        "todos": [{k: t[k] for k in ("id", "title", "state", "priority", "notes", "created_at", "updated_at", "created_by")}
                  | {"attachments": [{k: a.get(k) for k in ("kind", "title", "url", "artifact_path")} for a in t["attachments"]]}
                  for t in v["todos"]],
        "artifacts": [{k: a[k] for k in ("id", "kind", "title", "path", "canvas_id", "origin")} | ({"pinned": True} if a.get("pinned_at") else {})
                      for a in v["artifacts"]],
        "ledger": {kind: [{k: e.get(k) for k in ("id", "title", "status", "body", "rationale", "alternatives", "lesson", "what_worked",
                                                 "effective_cause", "effective_lesson", "made_by", "source", "created_at", "updated_at")
                           if e.get(k) not in (None, "", [])}
                          | ({"corrections": [{k: x.get(k) for k in ("cause", "lesson", "note", "source", "created_at") if x.get(k)}
                                              for x in e["corrections"]]} if e.get("corrections") else {})
                          for e in entries] for kind, entries in v["ledger"].items()},
        "children": [{"id": ch["id"], "title": ch["display_title"], "status": ch["status"], "counts": ch["counts"]}
                     | ({"task": _one_line(ch["task"])} if ch.get("task") else {})
                     | ({"result": _one_line(ch["result"])} if ch.get("result") and ch["status"] in FINISHED else {})
                     | ({"pinned": True} if ch.get("pinned_at") else {}) for ch in kids],
        "extraction": v["turns"],
    }
    if include:
        view = {k: val for k, val in view.items() if k in ("session", "breadcrumb") or k in include}
    return view


def _prune(node):
    return {"id": node["id"], "title": node["title"], "status": node["status"], "counts": node["counts"],
            "children": [_prune(ch) for ch in node["children"]]}


def call(name, args, meta):
    args = args or {}
    sid = args.get("session_id") or calling_session(meta)
    needs_sid = name in ("get_session", "get_tree", "add_todo", "delete_todo", "record", "add_artifact", "link_canvas", "get_digest", "extract_now")
    if needs_sid and not sid:
        raise ToolError("could not determine the calling session; pass session_id")
    q = urllib.parse.quote
    if name == "get_session":
        return _compact_view(api("GET", f"/api/sessions/{q(sid)}"), [x for x in args.get("include") or [] if x in SECTIONS])
    if name == "get_tree":
        root = api("GET", f"/api/sessions/{q(sid)}?rescan=0")["session"].get("root_id") or sid
        roots = api("GET", f"/api/tree?archived=1&root={q(root)}")["roots"]
        return {"calling_session": calling_session(meta), "tree": [_prune(r) for r in roots]}
    if name == "search":
        kinds = ",".join(args.get("kinds") or [])
        return api("GET", f"/api/search?q={q(args.get('query', ''))}&kinds={q(kinds)}")
    if name == "add_todo":
        return api("POST", f"/api/sessions/{q(sid)}/todos", {k: args[k] for k in ("title", "notes", "priority", "state") if k in args})
    if name == "update_todo":
        return api("PATCH", f"/api/todos/{int(args['todo_id'])}", {k: args[k] for k in ("title", "notes", "priority", "state") if k in args})
    if name == "delete_todo":
        return api("DELETE", f"/api/todos/{int(args['todo_id'])}", caller=calling_session(meta) or sid)
    if name == "attach_to_todo":
        if args.get("url"):
            body = {"kind": "url", "url": args["url"], "title": args.get("title")}
        elif args.get("artifact_path"):
            body = {"kind": "artifact", "path": args["artifact_path"], "title": args.get("title")}
        else:
            raise ToolError("url or artifact_path is required")
        return api("POST", f"/api/todos/{int(args['todo_id'])}/attachments", body)
    if name == "record":
        body = {k: args[k] for k in ("kind", "title", "body", "status", "rationale", "alternatives", "lesson") if k in args}
        body["made_by"] = "agent"
        return api("POST", f"/api/sessions/{q(sid)}/ledger", body)
    if name == "update_ledger":
        return api("PATCH", f"/api/ledger/{int(args['entry_id'])}", {k: args[k] for k in ("status", "title", "body", "rationale", "alternatives") if k in args})
    if name == "add_artifact":
        return api("POST", f"/api/sessions/{q(sid)}/artifacts", {"path": args["path"], "title": args.get("title")})
    if name == "link_canvas":
        return api("POST", f"/api/sessions/{q(sid)}/canvases", {"document_id": args["document_id"], "title": args.get("title")})
    if name == "get_digest":
        return api("GET", f"/api/digest?session={q(sid)}&event=SessionStart&source=resume")
    if name == "extract_now":
        return api("POST", f"/api/sessions/{q(sid)}/extract", {"only_missing": True})
    raise ToolError(f"unknown tool {name}")


def handle(msg):
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if method == "initialize":
        return {"protocolVersion": params.get("protocolVersion") or "2025-06-18", "capabilities": {"tools": {}},
                "serverInfo": {"name": "session-tracker", "version": __version__}, "instructions": INSTRUCTIONS}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        try:
            result = call(params.get("name"), params.get("arguments"), params.get("_meta"))
            return {"content": [{"type": "text", "text": json.dumps(result, indent=1, default=str)}]}
        except KeyError as e:
            return {"content": [{"type": "text", "text": f"Error: missing required argument {e}"}], "isError": True}
        except (ToolError, ValueError, TypeError) as e:
            return {"content": [{"type": "text", "text": f"Error: {e}"}], "isError": True}
    if method == "ping" or mid is not None:
        return {}
    return None


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if msg.get("id") is None:
            continue
        result = handle(msg)
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}) + "\n")
        sys.stdout.flush()
    return 0

