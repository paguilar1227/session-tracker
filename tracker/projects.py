"""Which project a session belongs to, for grouping, sorting and filtering in the UI.

Codex desktop keeps its own project list (name + root folder). Resolution order:
1. The session's git remote. If its folder is a git checkout, the checkout's current origin is used (none if it has
   no origin). If the folder exists but is not a checkout, there is none: older Codex builds stamped a remote on chats
   in folders that were never repositories. Only a folder that is gone (deleted worktrees, temp clones) keeps the
   remote Codex recorded.
2. A remote that matches a project's own remote (or, if that is unknown, the project folder's name) -> that project.
   This puts worktrees anywhere on disk under their repository.
3. Any other remote -> the repository name, even inside a broader project folder.
4. No remote: the deepest project folder containing the session's folder.
5. Fallbacks: temp folders, then the folder name.
Sessions in Codex's own chat folder (~/Documents/Codex) are "Chats (no project)" up front.

The service never reads or stats anything under macOS-protected folders (Documents, Desktop, Downloads, iCloud,
removable volumes): doing so from a background process raises a privacy prompt and blocks until it is answered.
"""
import functools
import os
import re
import time

CHATS = "Chats (no project)"
TEMP = "Temporary folders"
UNKNOWN = "Unknown"
PROTECTED = ("Documents", "Desktop", "Downloads", "Pictures", "Movies", "Music", "Library/Mobile Documents", "Library/CloudStorage")


def protected(path, home=None):
    """True for paths a background process may not touch without a macOS privacy prompt."""
    home = home or os.path.expanduser("~")
    path = (path or "").rstrip("/")
    return path.startswith("/Volumes/") or any(path == os.path.join(home, p) or path.startswith(os.path.join(home, p) + "/")
                                              for p in PROTECTED)


def repo_name(origin):
    """'https://github.com/o/toolbox.git' or 'git@host:o/toolbox.git' -> 'toolbox'."""
    return repo_key(origin).split("/")[-1]


def repo_key(origin):
    """Remote URL -> 'owner/repo' (lowercase), so https and ssh forms of the same remote compare equal."""
    if not origin:
        return ""
    parts = [p for p in re.split(r"[/:]", origin.strip().rstrip("/")) if p]
    if not parts:
        return ""
    parts[-1] = parts[-1][:-4] if parts[-1].endswith(".git") else parts[-1]
    return "/".join(parts[-2:]).lower()


def _config_path(dotgit):
    """Path of the git config for a .git directory, or for a worktree/submodule .git file (via gitdir and commondir)."""
    if os.path.isdir(dotgit):
        return os.path.join(dotgit, "config")
    try:
        line = open(dotgit, encoding="utf-8", errors="replace").read().strip()
    except OSError:
        return None
    if not line.startswith("gitdir:"):
        return None
    gitdir = os.path.join(os.path.dirname(dotgit), line[len("gitdir:"):].strip())
    try:
        common = open(os.path.join(gitdir, "commondir"), encoding="utf-8").read().strip()
        gitdir = os.path.join(gitdir, common)
    except OSError:
        pass
    return os.path.join(gitdir, "config")


def _origin(dotgit):
    """origin URL for a .git dir or file; '' when there is no origin; None when the config can't be read."""
    path = _config_path(dotgit)
    try:
        text = open(path, encoding="utf-8", errors="replace").read() if path else None
    except OSError:
        text = None
    if text is None:
        return None
    m = re.search(r'^\[remote "origin"\](.*?)(?=^\[|\Z)', text, re.M | re.S)
    url = re.search(r"^\s*url\s*=\s*(\S+)", m.group(1), re.M) if m else None
    return url.group(1) if url else ""


def git_remote(folder):
    """The origin URL of the repository at folder, or ''."""
    return _origin(os.path.join(folder, ".git")) or ""


def with_remotes(roots, home=None):
    """Add each project root's own origin remote (used to match sessions by remote); protected folders are not read."""
    return [dict(r, remote="" if protected(r["path"], home) else git_remote(r["path"])) for r in roots]


def _checkout(cwd):
    if not os.path.isdir(cwd):
        return "gone", None
    path = cwd
    while True:
        dotgit = os.path.join(path, ".git")
        if os.path.exists(dotgit):
            return "checkout", _origin(dotgit)
        parent = os.path.dirname(path)
        if parent == path:
            return "plain", None
        path = parent


@functools.lru_cache(maxsize=4096)
def _checkout_cached(cwd, minute):
    return _checkout(cwd)


def checkout(cwd):
    """('checkout', origin or '' or None if unreadable) | ('plain', None) | ('gone', None). Cached for about a minute."""
    return _checkout_cached(cwd, int(time.time() // 60))


def label(cwd, origin, roots, home=None):
    """roots: [{"name", "path", optional "remote"}] from Codex's project list."""
    home = home or os.path.expanduser("~")
    cwd = (cwd or "").rstrip("/")
    if cwd.startswith(os.path.join(home, "Documents", "Codex") + "/"):
        return CHATS
    if cwd and not protected(cwd, home):
        state, actual = checkout(cwd)
        if state == "plain":
            origin = None
        elif state == "checkout" and actual is not None:
            origin = actual or None
    key = repo_key(origin)
    if key:
        for r in roots:
            if r.get("remote") and repo_key(r["remote"]) == key:
                return r["name"]
        name = key.split("/")[-1]
        for r in roots:
            if not r.get("remote") and os.path.basename(r["path"].rstrip("/")).lower() == name:
                return r["name"]
        return repo_name(origin)
    best = None
    for r in roots:
        root = r["path"].rstrip("/")
        if cwd == root or cwd.startswith(root + "/"):
            if best is None or len(root) > len(best["path"].rstrip("/")):
                best = r
    if best:
        return best["name"]
    if not cwd:
        return UNKNOWN
    if re.match(r"^(/private)?/tmp/|^(/private)?/var/folders/", cwd + "/"):
        return TEMP
    return os.path.basename(cwd) or cwd
