"""Reload a thread's state into its context after Codex compacts it.

Main sessions get a SessionStart hook (source "compact") after compaction, which injects the digest. Subagents get only
PostCompact, and Codex's PostCompact hook cannot add context. So PostCompact leaves a marker named after the compacted
thread, and that thread's next PostToolUse hook, which can add context, injects the digest and removes the marker. The
marker survives the end of a turn, so a subagent that compacts and answers without another tool call is reloaded at its
first tool call in a later turn. None of these functions raise: a marker problem must never cost a session its digest.
"""
import os
import re

from . import config


def _dir():
    return config.DATA_DIR / "restore"


def _path(thread_id):
    name = re.sub(r"[^A-Za-z0-9_-]", "", thread_id or "")
    return _dir() / name if name else None


def mark(thread_id):
    path = _path(thread_id)
    if path:
        try:
            _dir().mkdir(parents=True, exist_ok=True)
            path.write_text(thread_id)
        except OSError:
            pass


def clear(thread_id):
    path = _path(thread_id)
    if path:
        try:
            path.unlink()
        except OSError:
            pass


def claim(thread_id):
    """True exactly once per marker, even if two hooks race for it."""
    path = _path(thread_id)
    if not path:
        return False
    taken = path.with_name(f"{path.name}.{os.getpid()}")
    try:
        os.rename(path, taken)
    except OSError:
        return False
    try:
        taken.unlink()
    except OSError:
        pass
    return True


def pending():
    try:
        return sorted(p.name for p in _dir().iterdir() if "." not in p.name)
    except OSError:
        return []


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def prune(c):
    """Drop markers of threads that are archived or whose root session is archived, and claim files left by a hook that
    died mid-claim. Other markers stay until claimed; the PostToolUse gate's cost does not depend on how many there are."""
    try:
        entries = list(_dir().iterdir())
    except OSError:
        return
    for p in entries:
        tid, _, pid = p.name.partition(".")
        if pid:
            if pid.isdigit() and not _alive(int(pid)):
                try:
                    p.unlink()
                except OSError:
                    pass
            continue
        row = c.execute("select s.archived, r.archived from sessions s left join sessions r on r.id = s.root_id where s.id=?",
                        (tid,)).fetchone()
        if row and (row[0] or row[1]):
            clear(tid)
