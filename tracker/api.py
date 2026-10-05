"""HTTP API, static UI, artifact serving and live updates (Server-Sent Events)."""
import json
import logging
import mimetypes
import os
import re
import subprocess
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import artifacts, canvas, config, db, digest, extractor, store

log = logging.getLogger("tracker.api")
MAX_UPLOAD = int(os.environ.get("ST_MAX_UPLOAD_BYTES", str(512 * 1024 * 1024)))
mimetypes.add_type("text/markdown", ".md")
mimetypes.add_type("text/markdown", ".markdown")
mimetypes.add_type("application/json", ".excalidraw")


class HttpError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


ROUTES = []


def route(method, pattern):
    def deco(fn):
        ROUTES.append((method, re.compile("^" + pattern + "$"), fn))
        return fn
    return deco


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        raise HttpError(400, "invalid id")


# ---------- read endpoints ----------

@route("GET", r"/api/health")
def health(h, c, m):
    pending = c.execute("select count(*) from turns where extract_status in ('pending','running')").fetchone()[0]
    failed = c.execute("select count(*) from turns where extract_status='failed'").fetchone()[0]
    last_fail = c.execute("select payload from events where type='turn.failed' order by id desc limit 1").fetchone()
    return {"ok": True, "codex_ok": db.get_meta("codex_ok", "None", c) == "True",
            "codex_problems": json.loads(db.get_meta("codex_problems", "[]", c)),
            "last_reconcile_at": int(db.get_meta("last_reconcile_at", "0", c)),
            "loop_error": db.get_meta("loop_error", "", c),
            "sessions": c.execute("select count(*) from sessions").fetchone()[0],
            "extractor": {"enabled": config.EXTRACT_ENABLED, "model": config.EXTRACT_MODEL, "effort": config.EXTRACT_EFFORT,
                          "pending": pending, "failed": failed,
                          "last_failure": json.loads(last_fail[0]) if last_fail else None,
                          "auto_since": int(db.get_meta("extract_since", "0", c))},
            "workflow_canvas_url": config.WORKFLOW_CANVAS_URL, "version": db.version(),
            "hooks": __import__("tracker.install", fromlist=["hook_trust"]).hook_trust(),
            "last_hook_at": (c.execute("select max(ts) from events where source='hook'").fetchone()[0] or 0),
            "hooks_changed_at": int(db.get_meta("hooks_changed_at", "0", c) or 0),
            "artifact_base": f"http://a{{id}}.{config.ARTIFACT_HOST}:{h.server.server_port}"}


@route("GET", r"/api/tree")
def get_tree(h, c, m):
    q = h.query
    return {"roots": store.tree(c, include_archived=q.get("archived") == "1", root_id=q.get("root") or None)}


@route("GET", r"/api/sessions/([\w\-]+)")
def get_session(h, c, m):
    return store.session_view(c, m.group(1), rescan=h.query.get("rescan", "1") != "0")


@route("GET", r"/api/digest")
def get_digest(h, c, m):
    q = h.query
    if not q.get("session"):
        raise HttpError(400, "session is required")
    return {"text": digest.build(c, q["session"], q.get("event") or "SessionStart", q.get("source"), q.get("parent"),
                                 int(q.get("reserve") or 0))}


@route("GET", r"/api/search")
def search(h, c, m):
    kinds = [k for k in (h.query.get("kinds") or "").split(",") if k]
    return {"results": store.search(c, h.query.get("q", ""), kinds or None)}


@route("GET", r"/api/canvas/documents")
def canvas_docs(h, c, m):
    try:
        return {"url": config.WORKFLOW_CANVAS_URL, "documents": canvas.list_documents()}
    except canvas.CanvasError as e:
        return {"url": config.WORKFLOW_CANVAS_URL, "documents": [], "error": str(e)}


@route("GET", r"/api/events")
def events(h, c, m):
    h.send_response(200)
    h.send_header("Content-Type", "text/event-stream")
    h.send_header("Cache-Control", "no-cache")
    h.send_header("Connection", "keep-alive")
    h.end_headers()
    seen = -1
    try:
        while True:
            v = db.wait_for_change(seen, timeout=25) if seen >= 0 else db.version()
            if v != seen:
                h.wfile.write(f"data: {json.dumps({'version': v})}\n\n".encode())
                seen = v
            else:
                h.wfile.write(b": ping\n\n")
            h.wfile.flush()
    except (BrokenPipeError, ConnectionResetError, OSError):
        return None
    return None


# ---------- todos ----------

@route("POST", r"/api/sessions/([\w\-]+)/todos")
def add_todo(h, c, m):
    with db.write(c):
        return store.create_todo(c, m.group(1), h.json(), created_by=h.actor)


@route("PATCH", r"/api/todos/(\d+)")
def patch_todo(h, c, m):
    with db.write(c):
        return store.update_todo(c, _int(m.group(1)), h.json(), actor=h.actor)


@route("POST", r"/api/todos/(\d+)/move")
def move_todo(h, c, m):
    with db.write(c):
        return store.move_todo(c, _int(m.group(1)), h.json(), actor=h.actor)


@route("POST", r"/api/sessions/([\w\-]+)/pin")
def pin_session(h, c, m):
    with db.write(c):
        return store.set_pinned(c, "session", m.group(1), bool(h.json().get("pinned")), actor=h.actor)


@route("POST", r"/api/artifacts/(\d+)/pin")
def pin_artifact(h, c, m):
    with db.write(c):
        return store.set_pinned(c, "artifact", _int(m.group(1)), bool(h.json().get("pinned")), actor=h.actor)


@route("DELETE", r"/api/todos/(\d+)")
def del_todo(h, c, m):
    with db.write(c):
        if h.actor == "agent":
            caller = h.headers.get("X-Caller-Session")
            t = store.get_todo(c, _int(m.group(1)))
            if not caller or not store.is_in_subtree(c, t["session_id"], caller):
                raise HttpError(403, "a session can only delete todos of its own session or its subagents")
        store.delete_todo(c, _int(m.group(1)), actor=h.actor)
    return {"ok": True}


@route("POST", r"/api/todos/(\d+)/attachments")
def add_attachment(h, c, m):
    data = h.json()
    with db.write(c):
        if data.get("kind") == "artifact" and data.get("path") and not data.get("artifact_id"):
            t = store.get_todo(c, _int(m.group(1)))
            data["artifact_id"] = artifacts.link_file(c, t["session_id"], data["path"], data.get("title"))["id"]
        return store.add_attachment(c, _int(m.group(1)), data)


@route("DELETE", r"/api/attachments/(\d+)")
def del_attachment(h, c, m):
    with db.write(c):
        store.delete_attachment(c, _int(m.group(1)))
    return {"ok": True}


@route("POST", r"/api/sessions/([\w\-]+)/references")
def upload_reference(h, c, m):
    name = urllib.parse.unquote(h.headers.get("X-Filename") or "upload")
    data = h.body(MAX_UPLOAD)
    sid = m.group(1)
    with db.write(c):
        store.get_session(c, sid)
        art = artifacts.save_reference(c, sid, name, data)
        att = None
        if h.query.get("todo"):
            att = store.add_attachment(c, _int(h.query["todo"]), {"kind": "reference", "artifact_id": art["id"]})
    return {"artifact": art, "attachment": att}


# ---------- ledger ----------

@route("POST", r"/api/sessions/([\w\-]+)/ledger")
def add_ledger(h, c, m):
    with db.write(c):
        return store.create_ledger(c, m.group(1), h.json(), source=h.actor)


@route("PATCH", r"/api/ledger/(\d+)")
def patch_ledger(h, c, m):
    with db.write(c):
        return store.update_ledger(c, _int(m.group(1)), h.json(), actor=h.actor)


@route("DELETE", r"/api/ledger/(\d+)")
def del_ledger(h, c, m):
    with db.write(c):
        store.delete_ledger(c, _int(m.group(1)), actor=h.actor)
    return {"ok": True}


def _user_only(h, what):
    if h.actor != "user":
        raise HttpError(403, f"Only you can {what} (from the UI).")


def _scribe(h):
    w = getattr(h.server, "worker", None)
    if w is not None:
        return w.client, w.src
    from .codex_source import CodexSource
    return extractor.Copilot(), CodexSource()


@route("POST", r"/api/ledger/(\d+)/corrections")
def add_correction(h, c, m):
    _user_only(h, "correct a mistake")
    with db.write(c):
        store.create_correction(c, _int(m.group(1)), h.json(), source="user")
        return store.get_ledger(c, _int(m.group(1)))


@route("POST", r"/api/ledger/(\d+)/recheck")
def recheck(h, c, m):
    _user_only(h, "ask the scribe to re-check a mistake")
    client, src = _scribe(h)
    try:
        out = extractor.recheck_mistake(c, client, src, _int(m.group(1)))
    except extractor.ExtractError as e:
        raise HttpError(502, str(e))
    out["entry"] = store.get_ledger(c, _int(m.group(1)))
    return out


@route("POST", r"/api/ledger/(\d+)/feedback")
def flag_wrong(h, c, m):
    _user_only(h, "flag scribe entries")
    with db.write(c):
        return store.create_feedback(c, {**(h.json() or {}), "kind": "wrong", "ledger_id": _int(m.group(1))})


@route("POST", r"/api/sessions/([\w\-]+)/feedback")
def report_missed(h, c, m):
    _user_only(h, "report missed mistakes")
    with db.write(c):
        return store.create_feedback(c, {**(h.json() or {}), "kind": "missed", "session_id": m.group(1)})


@route("GET", r"/api/feedback")
def list_feedback(h, c, m):
    return {"feedback": store.list_feedback(c, h.query.get("session") or None)}


@route("DELETE", r"/api/feedback/(\d+)")
def del_feedback(h, c, m):
    _user_only(h, "remove feedback")
    with db.write(c):
        store.delete_feedback(c, _int(m.group(1)))
    return {"ok": True}


@route("GET", r"/api/sessions/([\w\-]+)/turns")
def list_turns(h, c, m):
    store.get_session(c, m.group(1))
    _, src = _scribe(h)
    try:
        msgs = src.user_messages(m.group(1))
    except Exception:  # Codex history unavailable: still list the turns
        msgs = {}
    turns = store.session_turns(c, m.group(1))
    for t in turns:
        t["user_message"] = (msgs.get(t["turn_id"]) or "")[:200]
    return {"turns": turns}


@route("POST", r"/api/sessions/([\w\-]+)/extract")
def extract(h, c, m):
    data = h.json() or {}
    with db.write(c):
        store.get_session(c, m.group(1))
        n = extractor.queue_session(c, m.group(1), only_missing=data.get("only_missing", True))
    if h.server.worker:
        h.server.worker.wake.set()
    return {"queued": n}


# ---------- artifacts & canvases ----------

@route("POST", r"/api/sessions/([\w\-]+)/artifacts")
def link_artifact(h, c, m):
    data = h.json()
    try:
        with db.write(c):
            store.get_session(c, m.group(1))
            return artifacts.link_file(c, m.group(1), data.get("path") or "", data.get("title"))
    except FileNotFoundError:
        raise HttpError(400, "file not found: " + str(data.get("path")))


@route("POST", r"/api/sessions/([\w\-]+)/canvases")
def link_canvas(h, c, m):
    data = h.json()
    sid = m.group(1)
    doc_id, title = data.get("document_id"), data.get("title")
    if not doc_id:
        if not data.get("new_title"):
            raise HttpError(400, "document_id or new_title is required")
        try:
            doc = canvas.create_document(data["new_title"])
        except canvas.CanvasError as e:
            raise HttpError(502, str(e))
        doc_id, title = doc["documentId"], doc.get("title") or data["new_title"]
    with db.write(c):
        store.get_session(c, sid)
        return artifacts.link_canvas(c, sid, doc_id, title)


@route("DELETE", r"/api/artifacts/(\d+)")
def unlink_artifact(h, c, m):
    with db.write(c):
        a = c.execute("select * from artifacts where id=?", (_int(m.group(1)),)).fetchone()
        if not a:
            raise HttpError(404, "artifact not found")
        if a["origin"] not in ("linked", "canvas"):
            raise HttpError(400, "only linked files and canvases can be unlinked; folder files are managed on disk")
        c.execute("delete from artifacts where id=?", (a["id"],))
        db.unindex_doc(c, "artifact", a["id"])
    return {"ok": True}


def _reveal(path):
    subprocess.run(["open", "-R", path], check=False, timeout=10)


@route("POST", r"/api/artifacts/(\d+)/reveal")
def reveal(h, c, m):
    a = c.execute("select * from artifacts where id=?", (_int(m.group(1)),)).fetchone()
    if not a or not a["path"]:
        raise HttpError(404, "artifact has no file")
    if not os.path.exists(a["path"]):
        raise HttpError(404, "file is missing on disk")
    h.server.reveal(a["path"])
    return {"ok": True, "path": a["path"]}


@route("POST", r"/api/sessions/([\w\-]+)/reveal-folder")
def reveal_folder(h, c, m):
    with db.write(c):
        path = artifacts.session_dir(c, m.group(1))
    os.makedirs(path, exist_ok=True)
    h.server.reveal(path)
    return {"ok": True, "path": path}


class Handler(BaseHTTPRequestHandler):
    server_version = "SessionTracker/0.1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        log.debug("%s - %s", self.address_string(), fmt % args)

    # -- helpers --
    @property
    def actor(self):
        """Only the UI (which sends X-Actor: user) acts as the user; anything else, e.g. a script, is an agent."""
        return "user" if (self.headers.get("X-Actor") or "").lower() == "user" else "agent"

    def body(self, limit=10 * 1024 * 1024):
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit:
            raise HttpError(413, "request too large")
        self._body_read = True
        return self.rfile.read(n) if n else b""

    def end_headers(self):
        # A request body left unread (e.g. rejected before parsing) would be parsed as the next request on this
        # keep-alive connection. Close the connection instead so the client starts clean.
        if int(self.headers.get("Content-Length") or 0) > 0 and not getattr(self, "_body_read", False):
            self.send_header("Connection", "close")
            self.close_connection = True
        super().end_headers()

    def json(self):
        raw = self.body()
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except ValueError:
            raise HttpError(400, "invalid JSON")
        if not isinstance(data, dict):
            raise HttpError(400, "JSON object expected")
        return data

    def send_json(self, obj, code=200):
        data = json.dumps(obj, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def send_file(self, path, extra_headers=None, ctype=None):
        try:
            size = os.path.getsize(path)
            f = open(path, "rb")
        except OSError:
            raise HttpError(404, "file not found")
        with f:
            self.send_response(200)
            ctype = ctype or mimetypes.guess_type(path)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype in ("application/json", "application/javascript", "image/svg+xml"):
                ctype += "; charset=utf-8"
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "no-cache")
            for k, v in (extra_headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            while True:
                chunk = f.read(1024 * 256)
                if not chunk:
                    break
                self.wfile.write(chunk)

    @property
    def artifact_host(self):
        return f"{config.ARTIFACT_HOST}:{self.server.server_port}".lower()

    def _guard(self, method, path):
        """App origin: 127.0.0.1:port. Each HTML artifact gets its own origin, a<id>.localhost:port, which serves only GET /raw/<id>/*,
        so artifacts can't reach the app or read each other. The plain localhost:port origin redirects to those."""
        host = (self.headers.get("Host") or "").lower()
        port = self.server.server_port
        app_host = f"127.0.0.1:{port}"
        own = re.match(rf"^a(\d+)\.{re.escape(config.ARTIFACT_HOST.lower())}:{port}$", host)
        if own:
            if method == "GET" and re.match(rf"^/raw/{own.group(1)}(?:/|$)", path):
                return "artifact"
            raise HttpError(403, "an artifact origin only serves that artifact's own files")
        if host == self.artifact_host and host != app_host:
            raw = re.match(r"^/raw/(\d+)(?:/|$)", path)
            if method == "GET" and raw:
                self.send_response(302)
                self.send_header("Location", f"http://a{raw.group(1)}.{self.artifact_host}{self.path}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return None
            if method == "GET" and not path.startswith("/api/"):
                self.send_response(302)
                self.send_header("Location", f"http://{app_host}{self.path}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return None
            raise HttpError(403, "the artifact origin only serves files")
        if host != app_host:
            raise HttpError(403, "unknown host")
        origin = self.headers.get("Origin")
        if origin and urllib.parse.urlparse(origin).netloc.lower() != host:
            raise HttpError(403, "cross-origin request rejected")
        return "app"

    def _dispatch(self, method):
        self._body_read = False
        try:
            parsed = urllib.parse.urlparse(self.path)
            self.query = {k: v[-1] for k, v in urllib.parse.parse_qs(parsed.query).items()}
            path = parsed.path
            self.origin_kind = self._guard(method, path)
            if self.origin_kind is None:
                return
            if method == "GET" and not path.startswith("/api/"):
                return self._static(path)
            for meth, rx, fn in ROUTES:
                if meth != method:
                    continue
                m = rx.match(path)
                if m:
                    c = db.conn()
                    result = fn(self, c, m)
                    if result is not None:
                        self.send_json(result)
                    return
            raise HttpError(404, "not found")
        except HttpError as e:
            self._error(e.code, str(e))
        except KeyError as e:
            self._error(404, f"not found: {e.args[0] if e.args else ''}")
        except ValueError as e:
            self._error(400, str(e))
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # surface instead of hiding
            log.exception("request failed: %s %s", method, self.path)
            self._error(500, f"{type(e).__name__}: {e}")
        finally:
            db.close_thread()

    def _error(self, code, message):
        try:
            self.send_json({"error": message}, code)
        except OSError:
            pass

    def _static(self, path):
        if path.startswith("/raw/"):
            return self._raw(path)
        if path in ("/", "/index.html") or path.startswith("/s/"):
            return self.send_file(str(config.WEB_DIR / "index.html"))
        rel = path.lstrip("/")
        target = artifacts.safe_child(str(config.WEB_DIR), rel)
        if not target or not os.path.isfile(target):
            raise HttpError(404, "not found")
        return self.send_file(target)

    def _raw(self, path):
        """/raw/<artifact_id>/<name...>: the artifact file, or a sibling file inside its folder (relative links)."""
        m = re.match(r"^/raw/(\d+)(?:/(.*))?$", path)
        if not m:
            raise HttpError(404, "not found")
        c = db.conn()
        a = c.execute("select * from artifacts where id=?", (int(m.group(1)),)).fetchone()
        if not a or not a["path"]:
            raise HttpError(404, "artifact not found")
        sub = urllib.parse.unquote(m.group(2) or "")
        if not sub or sub == os.path.basename(a["path"]):
            target = a["path"]
        elif a["origin"] in ("folder", "reference"):
            # Relative links (images, CSS) resolve next to the artifact but must stay inside the session's folder.
            folder = c.execute("select artifacts_dir from sessions where id=?", (a["session_id"],)).fetchone()
            candidate = os.path.realpath(os.path.join(os.path.dirname(a["path"]), sub))
            target = candidate if folder and folder[0] and candidate.startswith(os.path.realpath(folder[0]) + os.sep) else None
        else:
            target = None  # linked files live anywhere on disk: serve only the file itself
        if not target or not os.path.isfile(target):
            raise HttpError(404, "file not found")
        headers = {"X-Content-Type-Options": "nosniff"}
        guessed = mimetypes.guess_type(target)[0] or ""
        if "html" in guessed or "svg" in guessed or "xml" in guessed:
            # On the artifact origin, scripts keep that (separate) origin so storage works; on the app origin
            # they are forced into an opaque origin. Either way they cannot act as the app.
            same = " allow-same-origin" if getattr(self, "origin_kind", "app") == "artifact" else ""
            headers["Content-Security-Policy"] = "sandbox allow-scripts allow-popups allow-forms allow-modals allow-downloads" + same
        ctype = "text/plain" if artifacts.kind_for(target) == "markdown" else None
        return self.send_file(target, headers, ctype)

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_DELETE(self):
        self._dispatch("DELETE")


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, worker=None, reveal=_reveal):
        super().__init__(addr, Handler)
        self.worker = worker
        self.reveal = reveal

