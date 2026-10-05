"""The long-running service: reconcile loop + spool ingest + extractor worker + HTTP server."""
import logging
import logging.handlers
import threading
import time
import traceback

from . import config, db, spool
from .api import Server
from .extractor import ExtractorWorker
from .reconcile import Reconciler

log = logging.getLogger("tracker")


def setup_logging():
    config.ensure_dirs()
    handler = logging.handlers.RotatingFileHandler(config.LOG_DIR / "service.log", maxBytes=5_000_000, backupCount=3)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    root.addHandler(logging.StreamHandler())


def first_run_setup(c):
    with db.write(c):
        if not db.get_meta("extract_since", None, c):
            db.set_meta("extract_since", db.now_ms(), c)
        if not db.get_meta("installed_at", None, c):
            db.set_meta("installed_at", db.now_ms(), c)
        if not db.get_meta("artifact_content_indexed", None, c):
            from . import artifacts
            for a in c.execute("select id, session_id, path, title, kind from artifacts where kind in ('markdown','html') and missing=0 and path is not null").fetchall():
                db.index_doc(c, "artifact", a["id"], a["session_id"], a["title"], a["path"] + " " + artifacts.searchable_text(a["path"], a["kind"]))
            db.set_meta("artifact_content_indexed", 1, c)


class Loop(threading.Thread):
    def __init__(self, reconciler=None, worker=None):
        super().__init__(name="reconcile", daemon=True)
        self.reconciler = reconciler or Reconciler()
        self.worker = worker
        self.stop_event = threading.Event()

    def run(self):
        c = db.conn()
        while not self.stop_event.is_set():
            t0 = time.monotonic()
            try:
                spool.ingest(c)
                result = self.reconciler.run_once(c)
                if result.get("threads") and self.worker:
                    self.worker.wake.set()
                if db.get_meta("loop_error", "", c):
                    with db.write(c):
                        db.set_meta("loop_error", "", c)
            except Exception as e:
                log.error("loop failed: %s\n%s", e, traceback.format_exc())
                try:
                    with db.write(c):
                        db.set_meta("loop_error", f"{type(e).__name__}: {e}", c)
                except Exception:
                    pass
            self.stop_event.wait(max(0.1, config.POLL_SECONDS - (time.monotonic() - t0)))


def serve():
    setup_logging()
    c = db.conn()
    db.init(c)
    first_run_setup(c)
    worker = ExtractorWorker() if config.EXTRACT_ENABLED else None
    if worker:
        worker.start()
    Loop(worker=worker).start()
    httpd = Server((config.HOST, config.PORT), worker=worker)
    log.info("session-tracker listening on http://%s:%s", config.HOST, config.PORT)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()

