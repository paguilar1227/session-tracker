"""Serve the tracker with made-up sessions: try the UI or take screenshots without touching your own data.

    python3 tools/demo.py [--port 8796]

Everything (a fake Codex home with sessions, subagents and turns, plus the tracker's database and artifacts) lives in a
temporary folder. The scribe is off. Ctrl-C stops the demo.
"""
import argparse
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
H = 3600
TICK = "\x60"


FOLDERS = {"/Users/dev/code/billing-service": "https://github.com/acme/billing-service.git",
           "/Users/dev/code/storefront": "https://github.com/acme/storefront.git"}


def rollout(base, tid, path, task):
    f = base / "rollouts" / f"{tid}.jsonl"
    f.parent.mkdir(parents=True, exist_ok=True)
    msg = {"type": "response_item", "payload": {"type": "agent_message", "author": "/root", "recipient": path, "content": [
        {"type": "input_text", "text": f"Message Type: NEW_TASK\nTask name: {path}\nSender: /root\nPayload:\n{task}"}]}}
    f.write_text(json.dumps({"type": "session_meta", "payload": {"id": tid}}) + "\n" + json.dumps(msg) + "\n")
    return str(f)


def build_codex(base, cx, now):
    billing, store_front = FOLDERS
    roots = [
        ("R1", "Ship CSV export for invoices", billing, 3 * H, 60, "inProgress"),
        ("R2", "Investigate the flaky checkout test", store_front, 26 * H, 22 * H, "completed"),
        ("R3", "Refactor the auth middleware", store_front, 50 * H, 47 * H, "completed"),
        ("R4", "Upgrade the Postgres client library", billing, 120 * H, 118 * H, "completed"),
    ]
    for tid, title, cwd, age, idle, status in roots:
        cx.thread(tid, title, cwd=cwd, created=now - age, updated=now - idle)
        cx.turn(tid, f"{tid}-t1", status, None if status == "inProgress" else now - idle)
    kids = [
        ("C1", "R1", "Hume", "/root/schema_migration", "Add an exported_at column to invoices with a reversible migration and run "
         "the migration tests up and down.", "completed", "Migration 0042 adds invoices.exported_at (nullable, indexed). Up and down "
         "ran on a copy of staging; 14 migration tests pass."),
        ("C2", "R1", "Curie", "/root/csv_writer", "Write a streaming CSV writer for invoices (RFC 4180 quoting) with unit tests.",
         "inProgress", "Writer and 22 tests done; wiring it into GET /invoices/export now."),
        ("C3", "R1", "Noether", "/root/export_benchmark", "Benchmark exporting 200k invoices and report wall time and peak memory.",
         "completed", "200k rows in 38 s with 92 MB peak memory when streaming; the in-memory version needed 1.4 GB. "
         "Report: export-benchmark.html"),
        ("C4", "R1", "Lovelace", "/root/api_docs", "Document GET /invoices/export in the public API reference.", "failed",
         "Stopped: the docs build needs a token for the private theme package."),
        ("C5", "R2", "Kepler", "/root/flake_hunt", "Run the checkout test 200 times and find what makes it fail.", "completed",
         "Fails 3/200: the test reads the cart before the async price update lands. Fixed by awaiting the price event."),
        ("C6", "R3", "Hopper", "/root/token_refresh", "Move token refresh out of the request path.", "completed",
         "Refresh now runs in a background task; p95 login latency down from 410 ms to 120 ms."),
    ]
    for tid, parent, nick, path, task, status, reply in kids:
        cwd = billing if parent in ("R1", "R4") else store_front
        age = {"R1": 2.5 * H, "R2": 25 * H, "R3": 49 * H}[parent]
        cx.thread(tid, task, parent=parent, nickname=nick, path=path, cwd=cwd, created=int(now - age),
                  updated=now - (90 if status == "inProgress" else int(age - 1800)), rollout=rollout(base, tid, path, task))
        cx.turn(tid, f"{tid}-t1", status, None if status == "inProgress" else int(now - age + 1500),
                error="HTTP 401 from the private package registry" if status == "failed" else None)
        cx.item(tid, f"{tid}-t1", f"{tid}-m1", "agentMessage", {"type": "agentMessage", "text": reply})
    with sqlite3.connect(cx.state) as st:
        st.execute("alter table threads add column git_origin_url text")
        for folder, remote in FOLDERS.items():
            st.execute("update threads set git_origin_url=? where cwd=?", (remote, folder))


def seed_tracker(c, store, db):
    with db.write(c):
        todos = [
            ("Stream invoices to CSV without loading every row", "P1", "in_progress",
             "Delegated to **/root/csv_writer** (Curie). Writer and tests are done; wiring into the endpoint next."),
            ("Fix the 504 on exports over 150k rows", "P1", "blocked",
             "Proxy closes idle responses after 30 s. Blocked until the streaming writer lands; see the open issue."),
            ("Benchmark a 200k-row export", "P1", "review", f"✅ 38 s, 92 MB peak. Report: {TICK}export-benchmark.html{TICK}."),
            ("Agree on the date format with finance", "P2", "waiting", "Asked finance: ISO 8601 or MM/DD/YYYY? Proposed ISO 8601."),
            ("Document GET /invoices/export", "P2", "not_started", "First attempt by **/root/api_docs** failed: the docs build needs a token."),
            ("Ask SRE for an export rate limit", "P3", "not_started", ""),
            ("Add an exported_at column", "P1", "done", "Migration 0042, tested up and down."),
        ]
        for title, prio, state, notes in todos:
            store.create_todo(c, "R1", {"title": title, "priority": prio, "state": state, "notes": notes},
                              created_by="extractor" if state != "not_started" else "agent")
        entries = [
            {"kind": "decision", "title": "Stream rows instead of building the file in memory", "status": "decided", "made_by": "user",
             "body": "Export writes CSV rows as they are read from the database.",
             "rationale": "The in-memory version needed 1.4 GB for 200k invoices; streaming stays under 100 MB.",
             "alternatives": ["Background job that uploads the file and emails a link", "Paginate in the client"]},
            {"kind": "decision", "title": "Use ISO 8601 dates in exports", "status": "proposed", "made_by": "agent",
             "rationale": "Unambiguous across locales and sorts correctly; waiting for finance to confirm."},
            {"kind": "issue", "title": "Exports over 150k rows time out with a 504", "status": "open",
             "body": "**Symptom:** 504 after 30 s on staging.\n\n**Cause (hypothesis):** the proxy closes idle responses after 30 s "
                     "and nothing is sent until the first page query finishes.\n\n**Repro:** export 200k invoices on staging.\n\n"
                     "**Fix (planned):** send the CSV header at once and flush rows as they are read."},
            {"kind": "issue", "title": "Migration 0042 hit a lock timeout on staging", "status": "resolved",
             "body": "**Symptom:** lock timeout while adding the index.\n\n**Cause:** a non-concurrent index on a busy table.\n\n"
                     "**Fix:** CREATE INDEX CONCURRENTLY in its own migration step. Re-ran up and down: clean."},
            {"kind": "mistake", "title": "Called the migration tested after running only the up step",
             "body": "The agent reported the migration as tested; only the up migration had run.",
             "rationale": "It treated a passing up migration as proof that rollback works.",
             "lesson": "Before calling a migration tested, run both up and down against a copy of real data and say which ones ran."},
        ]
        for e in entries:
            store.create_ledger(c, "R1", e, source="scribe")
        store.create_ledger(c, "R2", {"kind": "decision", "title": "Await the price event instead of sleeping in the test",
                                      "status": "decided", "rationale": "Removes the race without slowing the suite."}, source="scribe")
        store.create_todo(c, "R2", {"title": "Watch the checkout test on CI for a week", "priority": "P3", "state": "waiting"})
        store.set_pinned(c, "session", "C2", True)


def write_artifacts(c, artifacts):
    folder = Path(artifacts.session_dir(c, "R1"))
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "export-benchmark.html").write_text(
        "<!doctype html><meta charset=utf-8><title>Invoice export benchmark</title><body style='font:16px system-ui;margin:40px'>"
        "<h1>Invoice export benchmark</h1><p>200,000 invoices, staging copy.</p><table border=1 cellpadding=8>"
        "<tr><th>Version</th><th>Wall time</th><th>Peak memory</th></tr><tr><td>In memory</td><td>41 s</td><td>1.4 GB</td></tr>"
        "<tr><td>Streaming</td><td>38 s</td><td>92 MB</td></tr></table></body>")
    (folder / "export-plan.md").write_text(f"# CSV export plan\n\n1. Migration: {TICK}exported_at{TICK}\n2. Streaming writer\n"
                                           "3. Endpoint and docs\n4. Benchmark at 200k rows\n")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8796)
    a = ap.parse_args(argv)
    base = Path(tempfile.mkdtemp(prefix="st-demo-"))
    env = {"ST_DATA_DIR": str(base / "data"), "CODEX_HOME": str(base / "codex"), "ST_ARTIFACT_ROOT": str(base / "codex" / "visualizations"),
           "ST_PORT": str(a.port), "ST_BASE_URL": f"http://127.0.0.1:{a.port}", "ST_EXTRACT_ENABLED": "0",
           "ST_WORKFLOW_CANVAS_URL": "http://127.0.0.1:9"}
    os.environ.update(env)
    sys.path.insert(0, str(REPO))
    from tests.fakecodex import FakeCodex
    from tracker import artifacts, config, db, store
    from tracker.codex_source import CodexSource
    from tracker.reconcile import Reconciler
    config.ensure_dirs()
    cx = FakeCodex(base / "codex")
    now = int(time.time())
    build_codex(base, cx, now)
    c = db.connect()
    db.init(c)
    Reconciler(CodexSource(cx.state, cx.history)).run_once(c)
    seed_tracker(c, store, db)
    with db.write(c):
        write_artifacts(c, artifacts)
        artifacts.scan(c, "R1")
        art = c.execute("select id from artifacts where session_id='R1' and path like '%benchmark%'").fetchone()
        if art:
            store.set_pinned(c, "artifact", art[0], True)
    c.close()
    print(f"Demo data in {base}\nOpen http://127.0.0.1:{a.port}/s/R1  (Ctrl-C to stop)", flush=True)
    try:
        return subprocess.call([sys.executable, "-m", "tracker", "serve"], cwd=REPO, env=os.environ)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())

