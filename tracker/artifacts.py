"""Per-session artifact folders: Codex's per-thread folder under ~/.codex/visualizations."""
import datetime as dt
import os
import re
from pathlib import Path

from . import config, db

KINDS = {".html": "html", ".htm": "html", ".md": "markdown", ".markdown": "markdown",
         ".png": "image", ".jpg": "image", ".jpeg": "image", ".gif": "image", ".webp": "image", ".svg": "image",
         ".pdf": "pdf", ".excalidraw": "excalidraw"}
SKIP_DIRS = {"__pycache__", ".git", "node_modules", ".venv", ".playwright-cli"}
SKIP_EXT = {".pyc"}
SKIP_NAMES = {".DS_Store"}


_TAGS = re.compile(r"<(script|style)[^>]*>.*?</\1>|<[^>]+>|data:[^\s\"')]+", re.S | re.I)


def searchable_text(path, kind):
    """Plain text of Markdown/HTML artifacts for full-text search."""
    if kind not in ("markdown", "html"):
        return ""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return ""
    if kind == "html":
        import html as _html
        text = _html.unescape(_TAGS.sub(" ", text))
    return " ".join(text.split())


def kind_for(path):
    return KINDS.get(os.path.splitext(str(path))[1].lower(), "other")


def folder_index(root=None):
    """thread id -> existing folder, from ARTIFACT_ROOT/YYYY/MM/DD/<thread_id>."""
    root = Path(root or config.ARTIFACT_ROOT)
    idx = {}
    if not root.exists():
        return idx
    for p in root.glob("*/*/*/*"):
        if p.is_dir():
            idx[p.name] = str(p)
    return idx


def default_dir(session_id, created_at_ms):
    d = dt.datetime.fromtimestamp((created_at_ms or db.now_ms()) / 1000)
    return str(Path(config.ARTIFACT_ROOT) / f"{d:%Y}" / f"{d:%m}" / f"{d:%d}" / session_id)


def resolve_dir(c, session, index=None):
    """Existing Codex folder if there is one, otherwise the conventional path (created on demand)."""
    existing = (index or {}).get(session["id"])
    if not existing and session.get("artifacts_dir") and os.path.isdir(session["artifacts_dir"]):
        existing = session["artifacts_dir"]
    path = existing or session.get("artifacts_dir") or default_dir(session["id"], session.get("created_at"))
    if path != session.get("artifacts_dir"):
        c.execute("update sessions set artifacts_dir=? where id=?", (path, session["id"]))
    return path


def session_dir(c, session_id):
    s = c.execute("select * from sessions where id=?", (session_id,)).fetchone()
    if not s:
        raise KeyError(session_id)
    s = dict(s)
    return resolve_dir(c, s, folder_index() if not s.get("artifacts_dir") else None)


def _walk(base):
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for f in sorted(filenames):
            if f in SKIP_NAMES or os.path.splitext(f)[1].lower() in SKIP_EXT:
                continue
            yield os.path.join(dirpath, f)


def scan(c, session_id):
    """Sync the artifacts table with the session folder. Returns True if anything changed."""
    base = session_dir(c, session_id)
    seen = set()
    changed = False
    if os.path.isdir(base):
        for path in _walk(base):
            try:
                st = os.stat(path)
            except OSError:
                continue
            seen.add(path)
            rel = os.path.relpath(path, base)
            origin = "reference" if rel.split(os.sep)[0] == "references" else "folder"
            row = c.execute("select id,size,mtime,missing from artifacts where session_id=? and path=?",
                            (session_id, path)).fetchone()
            mtime = int(st.st_mtime * 1000)
            if row is None:
                cur = c.execute("""insert into artifacts(session_id,path,kind,title,size,mtime,origin,created_at)
                                   values(?,?,?,?,?,?,?,?)""",
                                (session_id, path, kind_for(path), rel, st.st_size, mtime, origin, db.now_ms()))
                db.index_doc(c, "artifact", cur.lastrowid, session_id, rel, path + " " + searchable_text(path, kind_for(path)))
                changed = True
            elif row["size"] != st.st_size or row["mtime"] != mtime or row["missing"]:
                c.execute("update artifacts set size=?, mtime=?, missing=0 where id=?", (st.st_size, mtime, row["id"]))
                db.index_doc(c, "artifact", row["id"], session_id, rel, path + " " + searchable_text(path, kind_for(path)))
                changed = True
    for row in c.execute("select id,path from artifacts where session_id=? and origin in ('folder','reference') and missing=0",
                         (session_id,)).fetchall():
        if row["path"] not in seen:
            # Gone from disk: drop it from search, and drop the row unless a todo still links to it (the link keeps its title).
            db.unindex_doc(c, "artifact", row["id"])
            if c.execute("select 1 from todo_attachments where artifact_id=? limit 1", (row["id"],)).fetchone():
                c.execute("update artifacts set missing=1 where id=?", (row["id"],))
            else:
                c.execute("delete from artifacts where id=?", (row["id"],))
            changed = True
    for row in c.execute("""select id from artifacts a where session_id=? and origin in ('folder','reference') and missing=1
                            and not exists (select 1 from todo_attachments t where t.artifact_id = a.id)""", (session_id,)).fetchall():
        db.unindex_doc(c, "artifact", row["id"])  # hidden rows whose last link was removed
        c.execute("delete from artifacts where id=?", (row["id"],))
        changed = True
    return changed


_SAFE = re.compile(r"[^A-Za-z0-9._ -]+")


def safe_name(name):
    name = os.path.basename(name or "").strip() or "upload"
    name = _SAFE.sub("_", name).strip(". ") or "upload"
    return name[:180]


def save_reference(c, session_id, filename, data):
    base = Path(session_dir(c, session_id)) / "references"
    base.mkdir(parents=True, exist_ok=True)
    name = safe_name(filename)
    stem, ext = os.path.splitext(name)
    target = base / name
    n = 1
    while target.exists():
        target = base / f"{stem}-{n}{ext}"
        n += 1
    with open(target, "xb") as f:
        f.write(data)
    scan(c, session_id)
    return dict(c.execute("select * from artifacts where session_id=? and path=?", (session_id, str(target))).fetchone())


def link_file(c, session_id, path, title=None):
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    st = os.stat(path)
    c.execute("""insert into artifacts(session_id,path,kind,title,size,mtime,origin,created_at) values(?,?,?,?,?,?,?,?)
                 on conflict(session_id,path) where path is not null do update set title=coalesce(excluded.title, artifacts.title), missing=0""",
              (session_id, path, kind_for(path), title or os.path.basename(path), st.st_size, int(st.st_mtime * 1000),
               "linked", db.now_ms()))
    row = dict(c.execute("select * from artifacts where session_id=? and path=?", (session_id, path)).fetchone())
    db.index_doc(c, "artifact", row["id"], session_id, row["title"], path + " " + searchable_text(path, row["kind"]))
    return row


def link_canvas(c, session_id, doc_id, title=None):
    c.execute("""insert into artifacts(session_id,kind,title,origin,canvas_id,created_at) values(?,?,?,?,?,?)
                 on conflict(session_id,canvas_id) where canvas_id is not null do update set title=coalesce(excluded.title, artifacts.title)""",
              (session_id, "canvas", title, "canvas", doc_id, db.now_ms()))
    row = dict(c.execute("select * from artifacts where session_id=? and canvas_id=?", (session_id, doc_id)).fetchone())
    db.index_doc(c, "artifact", row["id"], session_id, title or f"canvas {doc_id}", doc_id)
    return row


def safe_child(base, sub):
    """Resolve sub inside base; None if it escapes."""
    base = os.path.realpath(base)
    target = os.path.realpath(os.path.join(base, sub))
    if target == base or target.startswith(base + os.sep):
        return target
    return None

