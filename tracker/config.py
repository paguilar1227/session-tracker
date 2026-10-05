"""Paths and settings. Every value can be overridden with an environment variable."""
import glob
import os
import re
from pathlib import Path

HOME = Path.home()
PROJECT_DIR = Path(__file__).resolve().parent.parent
WEB_DIR = PROJECT_DIR / "web"


def _path(env, default):
    return Path(os.environ.get(env, default)).expanduser()


DATA_DIR = _path("ST_DATA_DIR", HOME / ".session-tracker")
DB_PATH = DATA_DIR / "tracker.sqlite"
SPOOL_DIR = DATA_DIR / "spool"
LOG_DIR = DATA_DIR / "logs"
CODEX_HOME = _path("CODEX_HOME", HOME / ".codex")
ARTIFACT_ROOT = _path("ST_ARTIFACT_ROOT", CODEX_HOME / "visualizations")
HOST = os.environ.get("ST_HOST", "127.0.0.1")
PORT = int(os.environ.get("ST_PORT", "8795"))
BASE_URL = os.environ.get("ST_BASE_URL", f"http://127.0.0.1:{PORT}")
# Artifacts are served from a different origin (same port, different host name) so their scripts get
# working storage without being able to reach the app's origin or its API.
ARTIFACT_HOST = os.environ.get("ST_ARTIFACT_HOST", "localhost")
WORKFLOW_CANVAS_URL = os.environ.get("ST_WORKFLOW_CANVAS_URL", "http://localhost:8790").rstrip("/")
WORKFLOW_CANVAS_SERVER = re.compile(os.environ.get("ST_WORKFLOW_CANVAS_SERVER_RE", r"workflow[-_ ]?canvas"), re.I)
POLL_SECONDS = float(os.environ.get("ST_POLL_SECONDS", "2"))
LABEL = os.environ.get("ST_LABEL", "local.session-tracker")
SKILL_SRC = PROJECT_DIR / "skills" / "session-tracker"

# The scribe calls GitHub Copilot with the gh CLI's token. The API host depends on the account's plan (individual, business,
# enterprise) and is read from GitHub's Copilot account endpoint unless ST_COPILOT_BASE is set.
COPILOT_BASE = os.environ.get("ST_COPILOT_BASE", "").rstrip("/")
GH_USER = os.environ.get("ST_GH_USER", "")
COPILOT_INTEGRATION_ID = os.environ.get("ST_COPILOT_INTEGRATION_ID", "copilot-developer-cli")
EXTRACT_MODEL = os.environ.get("ST_EXTRACT_MODEL", "claude-sonnet-5.5")
EXTRACT_EFFORT = os.environ.get("ST_EXTRACT_EFFORT", "high")
EXTRACT_ENABLED = os.environ.get("ST_EXTRACT_ENABLED", "1") != "0"

# Codex's documented default for model-visible hook context is ~2,500 tokens. Code-heavy text runs about
# 3 characters per token, so 2,500 x 3 = 7,500 characters stays under the limit even in the worst case.
DIGEST_MAX_CHARS = int(os.environ.get("ST_DIGEST_MAX_CHARS", "7500"))


def _latest(pattern):
    found = []
    for p in glob.glob(str(CODEX_HOME / pattern)):
        m = re.search(r"_(\d+)\.sqlite$", p)
        if m:
            found.append((int(m.group(1)), p))
    return Path(max(found)[1]) if found else None


def codex_state_db():
    return Path(os.environ["ST_CODEX_STATE_DB"]) if os.environ.get("ST_CODEX_STATE_DB") else _latest("state_*.sqlite")


def codex_history_db():
    return Path(os.environ["ST_CODEX_HISTORY_DB"]) if os.environ.get("ST_CODEX_HISTORY_DB") else _latest("thread_history_*.sqlite")


def ensure_dirs():
    for d in (DATA_DIR, SPOOL_DIR, LOG_DIR):
        d.mkdir(parents=True, exist_ok=True)

