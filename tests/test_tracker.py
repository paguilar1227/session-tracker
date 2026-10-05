"""Behavior tests against synthetic Codex databases and a real local HTTP server. Run: python3 -m unittest -v"""
import json
import os
import sqlite3
import tempfile
import sys
import threading
import time
import unittest
import unittest.mock
import urllib.error
import urllib.request
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="st-test-"))
os.environ.update({"ST_DATA_DIR": str(TMP / "data"), "CODEX_HOME": str(TMP / "codex"),
                   "ST_ARTIFACT_ROOT": str(TMP / "codex" / "visualizations"), "ST_EXTRACT_ENABLED": "1",
                   "ST_WORKFLOW_CANVAS_URL": "http://127.0.0.1:9"})

from tracker import api, artifacts, codex_source, config, db, delta, digest, extractor, hook, mcp_server, spool, store  # noqa: E402
from tracker.codex_source import CodexSource  # noqa: E402
from tracker.reconcile import Reconciler  # noqa: E402
from tests.fakecodex import NOW, FakeCodex  # noqa: E402

def fresh_db():
    """Point every thread (including HTTP handler threads) at a new database file."""
    config.DB_PATH = TMP / f"db-{time.time_ns()}.sqlite"
    db._local.conn = db.connect()
    db.init(db._local.conn)
    return db._local.conn


class FakeClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def max_prompt_tokens(self, model):
        return 200000

    def chat(self, model, system, user, effort):
        self.calls.append(user)
        return json.dumps(self.payload), {"prompt_tokens": 1}, 0.01


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.c = fresh_db()
        self.codex = FakeCodex(TMP / f"codex-{time.time_ns()}")
        self.rec = Reconciler(CodexSource(self.codex.state, self.codex.history))

    def test_tree_status_failed_rule_and_incremental(self):
        cx = self.codex
        cx.thread("R", "Root task")
        cx.thread("C", "child", parent="R", nickname="Hooke", path="/root/validate")
        cx.thread("G", "grandchild", parent="C", nickname="Pascal", path="/root/validate/deep")
        cx.turn("R", "r1", "completed", NOW - 10)
        cx.turn("C", "c1", "failed", NOW - 9, error="stream disconnected: dispatch_conflict")
        cx.turn("C", "c2", "completed", NOW - 8)
        cx.turn("G", "g1", "inProgress")
        self.assertEqual(self.rec.run_once(self.c)["threads"], 3)
        rows = {r["id"]: dict(r) for r in self.c.execute("select * from sessions")}
        self.assertEqual((rows["G"]["root_id"], rows["G"]["depth"]), ("R", 2))
        self.assertEqual(rows["R"]["status"], "waiting")
        self.assertEqual(rows["C"]["status"], "done")
        self.assertEqual(rows["G"]["status"], "running")
        issues = self.c.execute("select title, source from ledger where kind='issue'").fetchall()
        self.assertEqual(len(issues), 1)
        self.assertIn("dispatch_conflict", issues[0]["title"])
        self.assertEqual(self.rec.run_once(self.c)["threads"], 0, "G is re-checked but unchanged, so nothing is rewritten")
        cx.touch("C", NOW + 5)
        self.assertEqual(self.rec.run_once(self.c)["threads"], 1, "only C changed")
        self.assertEqual(self.c.execute("select count(*) from ledger where kind='issue'").fetchone()[0], 1)
        self.assertEqual(self.c.execute("select status from ledger where kind='issue'").fetchone()[0], "resolved",
                         "a completed turn after the failed one resolves the automatic issue")
        roots = store.tree(self.c)
        self.assertEqual([r["id"] for r in roots], ["R"])
        self.assertEqual(roots[0]["children"][0]["children"][0]["id"], "G")

    def test_subagent_task_and_result_are_recorded_and_shown(self):
        """An orchestrator needs each child's task and result after compaction; both come from Codex's own records."""
        cx = self.codex
        cx.thread("OR", "orchestrator")
        cx.thread("J1", "job", parent="OR", nickname="Hume", path="/root/j1")
        cx.thread("J2", "job", parent="OR", nickname="Kant", path="/root/j2")
        rollout = TMP / f"rollout-{time.time_ns()}.jsonl"
        lines = [{"type": "session_meta", "payload": {"id": "J1"}},
                 {"type": "response_item", "payload": {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "context"}]}},
                 {"type": "response_item", "payload": {"type": "agent_message", "author": "/root", "recipient": "/root/j1", "content": [
                     {"type": "input_text", "text": "Message Type: NEW_TASK\nTask name: /root/j1\nSender: /root\nPayload:\nFind the flaky test in ci.yml"}]}},
                 {"type": "response_item", "payload": {"type": "reasoning"}},
                 {"type": "response_item", "payload": {"type": "agent_message", "content": [{"type": "input_text", "text": "Message Type: NEW_TASK\nPayload:\nlater follow-up"}]}}]
        rollout.write_text("\n".join(json.dumps(x) for x in lines))
        with sqlite3.connect(cx.state) as st:
            st.execute("update threads set rollout_path=? where id='J1'", (str(rollout),))
        cx.turn("J1", "j1t", "completed", NOW - 5)
        cx.item("J1", "j1t", "m1", "agentMessage", {"type": "agentMessage", "text": "working on it"})
        cx.item("J1", "j1t", "m2", "agentMessage", {"type": "agentMessage", "text": "J1 RESULT: test_upload is flaky, fixed in commit abc1234"})
        cx.turn("J2", "j2t", "inProgress")
        cx.item("J2", "j2t", "m3", "agentMessage", {"type": "agentMessage", "text": "halfway"})
        self.rec.run_once(self.c)
        rows = {r["id"]: dict(r) for r in self.c.execute("select id, task, result from sessions where id in ('J1','J2')")}
        self.assertEqual(rows["J1"]["task"], "Find the flaky test in ci.yml", "the first NEW_TASK, not a later follow-up")
        self.assertEqual(rows["J1"]["result"], "J1 RESULT: test_upload is flaky, fixed in commit abc1234")
        self.assertIsNone(rows["J2"]["task"], "no rollout yet and still running: left unset so a later cycle reads it")
        text = digest.build(self.c, "OR")
        lines = [ln for ln in text.splitlines() if "(session J" in ln]
        self.assertTrue(lines[0].startswith("- Kant") and ("running" in lines[0] or "stalled" in lines[0]), "active children come first")
        self.assertNotIn("halfway", text, "an unfinished child's partial message is not shown as a result")
        self.assertIn("task: Find the flaky test in ci.yml — result: J1 RESULT: test_upload is flaky, fixed in commit abc1234", text)
        kids = mcp_server._compact_view(store.session_view(self.c, "OR", rescan=False))["children"]
        j1 = next(k for k in kids if k["id"] == "J1")
        self.assertEqual((j1["task"], j1["result"]), ("Find the flaky test in ci.yml", "J1 RESULT: test_upload is flaky, fixed in commit abc1234"))

    def test_subagent_task_written_after_the_first_cycle_is_still_recorded(self):
        """Codex can register a child before its NEW_TASK reaches the rollout; the task must be read on a later cycle."""
        cx = self.codex
        cx.thread("OR2", "orchestrator")
        cx.thread("K1", "job", parent="OR2", nickname="Hume", path="/root/k1")
        rollout = TMP / f"rollout-{time.time_ns()}.jsonl"
        rollout.write_text(json.dumps({"type": "session_meta", "payload": {"id": "K1"}}) + "\n")
        with sqlite3.connect(cx.state) as st:
            st.execute("update threads set rollout_path=? where id='K1'", (str(rollout),))
        cx.turn("K1", "k1t", "inProgress")
        self.rec.run_once(self.c)
        self.assertIsNone(self.c.execute("select task from sessions where id='K1'").fetchone()[0])
        with rollout.open("a") as f:
            f.write(json.dumps({"type": "response_item", "payload": {"type": "agent_message", "author": "/root", "recipient": "/root/k1",
                    "content": [{"type": "input_text", "text": "Message Type: NEW_TASK\nTask name: /root/k1\nSender: /root\nPayload:\nPort the CLI"}]}}) + "\n")
        cx.item("K1", "k1t", "k1m", "agentMessage", {"type": "agentMessage", "text": "porting"})
        cx.touch("K1", NOW + 5)
        self.rec.run_once(self.c)
        self.assertEqual(self.c.execute("select task from sessions where id='K1'").fetchone()[0], "Port the CLI")

    def test_forked_subagent_task_skips_the_copied_parent_history(self):
        """A forked child's rollout starts with the parent's turns (model output, tasks for other agents); its own task comes after."""
        rollout = TMP / f"rollout-{time.time_ns()}.jsonl"
        msg = lambda to, body: {"type": "response_item", "payload": {"type": "agent_message", "author": "/root", "recipient": to, "content": [
            {"type": "input_text", "text": f"Message Type: NEW_TASK\nTask name: {to}\nSender: /root\nPayload:\n{body}"}]}}
        lines = [{"type": "session_meta", "payload": {"id": "F1", "forked_from_id": "P"}},
                 {"type": "session_meta", "payload": {"id": "P"}},
                 {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "parent talk"}]}},
                 msg("/root/other", "someone else's task"),
                 {"type": "response_item", "payload": {"type": "reasoning"}},
                 msg("/root/f1", "Audit the router"),
                 msg("/root/f1", "a later follow-up")]
        rollout.write_text("\n".join(json.dumps(x) for x in lines))
        self.assertEqual(codex_source.spawn_task(str(rollout), "/root/f1"), "Audit the router")
        self.assertIsNone(codex_source.spawn_task(str(rollout), "/root/missing"), "not addressed yet: read again later")

    def test_get_session_shows_results_only_for_finished_children_and_can_return_just_the_board(self):
        """A running child's latest message is progress, not a result; status reads can skip artifacts and the ledger."""
        cx = self.codex
        cx.thread("OR3", "orchestrator")
        cx.thread("L0", "job", parent="OR3", nickname="Hart", path="/root/l0")
        cx.thread("L1", "job", parent="OR3", nickname="Mill", path="/root/l1")
        cx.thread("L2", "job", parent="OR3", nickname="Rawls", path="/root/l2")
        with sqlite3.connect(cx.state) as st:
            for tid, age in (("L0", 300), ("L1", 200), ("L2", 100)):
                st.execute("update threads set created_at_ms=? where id=?", ((NOW - age) * 1000, tid))
        cx.turn("L0", "l0t", "completed", NOW - 5)
        cx.turn("L1", "l1t", "inProgress")
        cx.item("L1", "l1t", "l1m", "agentMessage", {"type": "agentMessage", "text": "halfway there"})
        cx.turn("L2", "l2t", "completed", NOW - 5)
        cx.item("L2", "l2t", "l2m", "agentMessage", {"type": "agentMessage", "text": "L2 RESULT finished"})
        self.rec.run_once(self.c)
        view = store.session_view(self.c, "OR3", rescan=False)
        kids = mcp_server._compact_view(view)["children"]
        self.assertEqual([k["id"] for k in kids], ["L1", "L2", "L0"], "running first, then newest, as in the digest")
        self.assertNotIn("result", kids[0])
        self.assertEqual(kids[1]["result"], "L2 RESULT finished")
        own = mcp_server._compact_view(store.session_view(self.c, "L1", rescan=False))["session"]
        self.assertNotIn("result", own, "a running session's latest message is not its result in its own view either")
        self.assertEqual(self.c.execute("select task from sessions where id='L0'").fetchone()[0], "",
                         "finished without a readable task: recorded as empty so it is not re-read every cycle")
        slim = mcp_server._compact_view(view, ["todos", "children"])
        self.assertEqual(set(slim), {"session", "breadcrumb", "todos", "children"})

    def test_empty_tasks_are_reset_for_a_reread_once(self):
        self.c.execute("insert into sessions(id, parent_id, created_at, updated_at, status, task) values('E1', 'OR', 1, 1, 'done', '')")
        self.c.execute("delete from meta where key='task_reread_v1'")
        db.init(self.c)
        self.assertIsNone(self.c.execute("select task from sessions where id='E1'").fetchone()[0])
        self.c.execute("update sessions set task='' where id='E1'")
        db.init(self.c)
        self.assertEqual(self.c.execute("select task from sessions where id='E1'").fetchone()[0], "", "the reset runs once")

    def test_archiving_in_codex_is_picked_up_without_an_update(self):
        cx = self.codex
        cx.thread("ARC", "to be archived")
        self.rec.run_once(self.c)
        self.assertEqual(self.c.execute("select archived from sessions where id='ARC'").fetchone()[0], 0)
        with sqlite3.connect(cx.state) as st:  # Codex flips the flag without touching updated_at
            st.execute("update threads set archived=1 where id='ARC'")
        self.rec.run_once(self.c)
        self.assertEqual(self.c.execute("select archived from sessions where id='ARC'").fetchone()[0], 1)
        self.assertNotIn("ARC", [r["id"] for r in store.tree(self.c)])
        with sqlite3.connect(cx.state) as st:
            st.execute("update threads set archived=0 where id='ARC'")
        self.rec.run_once(self.c)
        self.assertEqual(self.c.execute("select archived from sessions where id='ARC'").fetchone()[0], 0, "unarchiving is picked up too")

    def test_tree_nodes_carry_project_and_subtree_activity(self):
        cx = self.codex
        with sqlite3.connect(cx.state) as st:
            st.executescript("""alter table threads add column git_origin_url text;
              create table projects(id text primary key, name text, metadata text, position int, created_at_ms int, updated_at_ms int);
              create table project_roots(project_id text, position int, path text, primary key(project_id, position));
              insert into projects values('p1','Inventory','{}',0,0,0);
              insert into project_roots values('p1',0,'/work/inventory-app');""")
        cx.thread("WT", "worktree session", updated=NOW - 500)
        cx.thread("KID", "child", parent="WT", updated=NOW - 5)
        cx.thread("TMP", "scratch")
        with sqlite3.connect(cx.state) as st:
            st.execute("update threads set cwd='/elsewhere/wt-7/inventory-app', git_origin_url='https://github.com/o/inventory-app.git' where id in ('WT','KID')")
            st.execute("update threads set cwd='/private/tmp/x.abc', git_origin_url='' where id='TMP'")
        self.rec.run_once(self.c)
        roots = {r["id"]: r for r in store.tree(self.c)}
        self.assertEqual(roots["WT"]["project"], "Inventory", "a worktree outside the project folder is matched by its git remote")
        self.assertEqual(roots["WT"]["children"][0]["project"], "Inventory")
        self.assertEqual(roots["TMP"]["project"], "Temporary folders")
        self.assertEqual(roots["WT"]["activity_at"], (NOW - 5) * 1000, "a root's activity includes its children")
        self.assertEqual(roots["WT"]["children"][0]["activity_at"], (NOW - 5) * 1000)
        self.assertEqual(store.get_session(self.c, "KID")["project"], "Inventory")

    def test_stalled_running_session_is_labelled_and_recovers(self):
        cx = self.codex
        cx.thread("DONE", "finished work")
        cx.turn("DONE", "d1", "completed", NOW - 100)
        with sqlite3.connect(cx.history) as h:  # a completed turn whose longest pause is 10 minutes
            for n, ms in enumerate((NOW - 2000, NOW - 1400)):
                h.execute("insert into thread_items values(?,?,?,?,?,?,?)", ("DONE", "d1", f"i{n}", 900 + n, ms * 1000, "{}", "agentMessage"))
        cx.thread("OLD", "abandoned", updated=NOW - 3600)
        cx.turn("OLD", "o1", "inProgress")
        cx.thread("LIVE", "busy", updated=NOW - 60)
        cx.turn("LIVE", "l1", "inProgress")
        self.rec.run_once(self.c)
        status = dict(self.c.execute("select id, status from sessions").fetchall())
        self.assertEqual(self.rec.stall_after_ms, 600_000)
        self.assertEqual((status["OLD"], status["LIVE"]), ("stalled", "running"))
        cx.touch("OLD", int(time.time()))
        self.rec.run_once(self.c)
        self.assertEqual(self.c.execute("select status from sessions where id='OLD'").fetchone()[0], "running")

    def test_running_sessions_are_rechecked_without_thread_change(self):
        self.codex.thread("R", "Root")
        self.codex.turn("R", "r1", "inProgress")
        self.rec.run_once(self.c)
        self.assertEqual(self.c.execute("select status from sessions where id='R'").fetchone()[0], "running")
        with sqlite3.connect(self.codex.history) as h:
            h.execute("update thread_turns set status='completed', completed_at=? where turn_id='r1'", (NOW,))
        self.rec.run_once(self.c)
        self.assertEqual(self.c.execute("select status from sessions where id='R'").fetchone()[0], "waiting")

    def test_no_connection_leak_under_launchd_fd_limit(self):
        """Regression: Codex DB connections must be closed (launchd's soft limit is 256 open files)."""
        import resource
        for i in range(150):
            self.codex.thread(f"T{i}", f"thread {i}")
            self.codex.turn(f"T{i}", f"t{i}", "completed", NOW - 1)
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        open_now = len(os.listdir("/dev/fd"))
        resource.setrlimit(resource.RLIMIT_NOFILE, (open_now + 40, hard))
        try:
            result = self.rec.run_once(self.c)
        finally:
            resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))
        self.assertEqual(result["threads"], 150)

    def test_idle_cycles_do_not_signal_changes(self):
        self.codex.thread("R", "Root")
        self.codex.turn("R", "r1", "inProgress")
        self.rec.run_once(self.c)
        v = db.version()
        for _ in range(3):
            self.rec.run_once(self.c)
            with db.write(self.c):
                artifacts.scan(self.c, "R")
        self.assertEqual(db.version(), v, "nothing changed, so no change events")
        with db.write(self.c):
            db.set_meta("threads_watermark", 10**15, self.c)
        self.codex.thread("Z", "late", updated=NOW + 9)
        self.rec.run_once(self.c)
        self.assertEqual(int(db.get_meta("threads_watermark", "0", self.c)), 10**15, "watermark never moves backwards")

    def test_extraction_only_for_turns_after_install(self):
        with db.write(self.c):
            db.set_meta("extract_since", (NOW - 5) * 1000, self.c)
        self.codex.thread("R", "Root")
        self.codex.turn("R", "old", "completed", NOW - 30)
        self.codex.turn("R", "new", "completed", NOW - 1)
        self.rec.run_once(self.c)
        st = dict(self.c.execute("select turn_id, extract_status from turns").fetchall())
        self.assertEqual(st, {"old": "skipped", "new": "pending"})
        self.assertEqual(extractor.queue_session(self.c, "R"), 1)

    def test_canvas_links_only_targeted_documents(self):
        cx = self.codex
        cx.thread("R", "Root")
        cx.turn("R", "t1", "completed", NOW)
        listing = {"documents": [{"id": "a"}, {"id": "b"}]}
        cx.item("R", "t1", "i1", "mcpToolCall", {"server": "workflow-canvas", "tool": "list_documents", "arguments": {},
                                                 "result": {"content": [{"type": "text", "text": json.dumps(listing)}]}})
        cx.item("R", "t1", "i2", "mcpToolCall", {"server": "workflow-canvas", "tool": "add_nodes", "arguments": {"documentId": "doc1"},
                                                 "result": {"content": [{"type": "text", "text": json.dumps({"ok": True, "documentId": "doc1"})}]}})
        cx.item("R", "t1", "i3", "mcpToolCall", {"server": "workflow-canvas", "tool": "create_document", "arguments": {"title": "Plan"},
                                                 "result": {"content": [{"type": "text", "text": json.dumps({"ok": True, "documentId": "doc2", "title": "Plan"})}]}})
        cx.item("R", "t1", "i4", "mcpToolCall", {"server": "other", "tool": "x", "arguments": {"documentId": "nope"}})
        self.rec.run_once(self.c)
        linked = {r["canvas_id"]: r["title"] for r in self.c.execute("select canvas_id, title from artifacts where kind='canvas'")}
        self.assertEqual(linked, {"doc1": None, "doc2": "Plan"})


class ProjectLabelTests(unittest.TestCase):
    def test_label_resolution_order(self):
        from tracker import projects
        roots = [{"name": "workspace", "path": "/w/p"}, {"name": "toolbox", "path": "/w/p/toolbox"},
                 {"name": "Inventory", "path": "/w/p/inventory-app"}]
        self.assertEqual(projects.repo_key("git@github.com-work:acme-org/webapp.git"), "acme-org/webapp")
        self.assertEqual(projects.repo_key("https://github.com/acme-org/webapp"), "acme-org/webapp")
        self.assertEqual(projects.label("/w/p/toolbox-worktrees/x", "https://github.com/o/toolbox.git", roots), "toolbox",
                         "a remote beats the enclosing parent folder (paths that no longer exist keep their remote)")
        self.assertEqual(projects.label("/w/p/clock-app", "https://github.com/o/clock-app.git", roots), "clock-app",
                         "a repo nested in a broad project folder keeps its own name")
        self.assertEqual(projects.label("/w/p/inventory-app/sub", None, roots), "Inventory", "deepest enclosing project folder")
        self.assertEqual(projects.label("/w/p/notes", None, roots), "workspace")
        self.assertEqual(projects.label("/x/y", "https://github.com/o/test-w-repo.git", roots), "test-w-repo")
        with_remote = roots + [{"name": "Workspace", "path": "/w/q", "remote": "git@github.com:o/test-w-repo.git"}]
        self.assertEqual(projects.label("/x/y", "https://github.com/o/test-w-repo.git", with_remote), "Workspace",
                         "matched by the project's own remote, not its folder name")
        self.assertEqual(projects.label("/h/Documents/Codex/2026-10-03/slug", None, roots, home="/h"), projects.CHATS)
        self.assertEqual(projects.label("/var/folders/ab/T/run", None, roots), projects.TEMP)
        self.assertEqual(projects.label("/h/.buzz", None, roots, home="/h"), ".buzz")
        self.assertEqual(projects.label(None, None, roots), projects.UNKNOWN)

    def test_protected_folders_are_never_touched(self):
        from tracker import projects
        touched = []
        real = projects._checkout
        projects._checkout = lambda cwd: touched.append(cwd) or ("plain", None)
        projects._checkout_cached.cache_clear()
        try:
            self.assertEqual(projects.label("/h/Documents/Codex/2026-08-10/voice", "https://github.com/o/x.git", [], home="/h"), projects.CHATS)
            self.assertEqual(projects.label("/h/Documents/repos/app", "https://github.com/o/app.git", [], home="/h"), "app",
                             "a protected folder keeps its recorded remote without being inspected")
            self.assertEqual(projects.label("/h/Library/Mobile Documents/x", None, [], home="/h"), "x")
            self.assertEqual(projects.label("/Volumes/USB/proj", None, [], home="/h"), "proj")
            self.assertEqual(projects.with_remotes([{"name": "D", "path": "/h/Desktop/d"}], home="/h")[0]["remote"], "")
        finally:
            projects._checkout = real
            projects._checkout_cached.cache_clear()
        self.assertEqual(touched, [], "nothing under Documents, iCloud or /Volumes is stat-ed or read")

    def test_stale_remote_on_a_plain_folder_is_ignored(self):
        from tracker import projects
        home = TMP / f"home-{time.time_ns()}"
        chat = home / "Documents" / "Codex" / "2026-09-01" / "slug"
        repo = home / "work" / "repo" / "sub"
        chat.mkdir(parents=True)
        repo.mkdir(parents=True)
        (home / "work" / "repo" / ".git").mkdir()
        (home / "work" / "repo" / ".git" / "config").write_text('[core]\n\tbare = false\n[remote "origin"]\n\turl = git@github.com:o/test-w-repo.git\n')
        stale = "https://github.com/o/test-w-repo.git"
        self.assertEqual(projects.label(str(chat), stale, [], home=str(home)), projects.CHATS,
                         "a Codex chat folder that is not a git checkout ignores the remote Codex stamped on it")
        self.assertEqual(projects.label(str(home / ".buzz"), stale, [], home=str(home)), "test-w-repo", "a folder that is gone keeps its remote")
        (home / ".buzz").mkdir()
        projects._checkout_cached.cache_clear()
        self.assertEqual(projects.label(str(home / ".buzz"), stale, [], home=str(home)), projects.TEMP,
                         "the remote is ignored once the folder exists without a checkout (this test home lives in the temp dir)")
        roots = projects.with_remotes([{"name": "Workspace", "path": str(home / "work" / "repo")}])
        self.assertEqual(roots[0]["remote"], "git@github.com:o/test-w-repo.git")
        self.assertEqual(projects.label(str(repo), stale, roots, home=str(home)), "Workspace")
        inited = home / "Documents" / "Codex" / "2026-08-10" / "voice"
        (inited / ".git").mkdir(parents=True)
        (inited / ".git" / "config").write_text("[core]\n\tbare = false\n")
        self.assertEqual(projects.label(str(inited), stale, roots, home=str(home)), projects.CHATS,
                         "a Codex chat folder is a chat even when it was git-inited")
        plain_repo = home / "src" / "inited"
        (plain_repo / ".git").mkdir(parents=True)
        (plain_repo / ".git" / "config").write_text("[core]\n\tbare = false\n")
        self.assertEqual(projects.label(str(plain_repo), stale, roots, home=str(home)), projects.TEMP,
                         "a checkout with no origin ignores the remote Codex recorded (this test home lives in the temp dir)")
        main = home / "work" / "tools"
        (main / ".git" / "worktrees" / "wt1").mkdir(parents=True)
        (main / ".git" / "config").write_text('[remote "origin"]\n\turl = https://github.com/o/toolbox.git\n')
        (main / ".git" / "worktrees" / "wt1" / "commondir").write_text("../..\n")
        wt = home / "work" / "tools-worktrees" / "wt1"
        wt.mkdir(parents=True)
        (wt / ".git").write_text(f"gitdir: {main / '.git' / 'worktrees' / 'wt1'}\n")
        self.assertEqual(projects.label(str(wt), None, [{"name": "toolbox", "path": str(main), "remote": ""}], home=str(home)),
                         "toolbox", "a worktree's .git file leads to the main checkout's origin")


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.c = fresh_db()
        sid = f"S{time.time_ns()}"
        self.sid = sid
        self.dir = Path(config.ARTIFACT_ROOT) / "2026" / "10" / "03" / sid
        (self.dir / "__pycache__").mkdir(parents=True)
        (self.dir / "report.html").write_text("<h1>hi</h1>")
        (self.dir / "notes.md").write_text("# Notes :rocket:")
        (self.dir / "x.pyc").write_bytes(b"0")
        (self.dir / "__pycache__" / "y.pyc").write_bytes(b"0")
        with db.write(self.c):
            self.c.execute("insert into sessions(id, created_at, updated_at) values(?,?,?)", (sid, db.now_ms(), db.now_ms()))

    def test_scan_reference_and_traversal(self):
        with db.write(self.c):
            artifacts.scan(self.c, self.sid)
        kinds = sorted(r["kind"] for r in self.c.execute("select kind from artifacts where session_id=?", (self.sid,)))
        self.assertEqual(kinds, ["html", "markdown"])
        (self.dir / "findings.html").write_text("<style>.x{}</style><h1>Quarterly zebrafish findings</h1><script>var q=1</script>")
        with db.write(self.c):
            artifacts.scan(self.c, self.sid)
        hits = store.search(self.c, "zebrafish")
        self.assertEqual([h["kind"] for h in hits], ["artifact"])
        with db.write(self.c):
            a = artifacts.save_reference(self.c, self.sid, "../../Spec Doc.pdf", b"%PDF")
            b = artifacts.save_reference(self.c, self.sid, "Spec Doc.pdf", b"%PDF")
        self.assertEqual(Path(a["path"]).parent, self.dir / "references")
        self.assertEqual(Path(b["path"]).name, "Spec Doc-1.pdf")
        self.assertEqual(a["origin"], "reference")
        self.assertIsNone(artifacts.safe_child(str(self.dir), "../../etc/passwd"))
        (self.dir / "notes.md").unlink()
        with db.write(self.c):
            artifacts.scan(self.c, self.sid)
        self.assertIsNone(self.c.execute("select missing from artifacts where path like '%notes.md'").fetchone(), "an unlinked file that is gone leaves no row")


    def test_pins_survive_a_rescan_and_a_codex_sync(self):
        artifacts.scan(self.c, self.sid)
        report = self.c.execute("select id from artifacts where session_id=? and title='report.html'", (self.sid,)).fetchone()[0]
        with db.write(self.c):
            store.set_pinned(self.c, "artifact", report, True)
            store.set_pinned(self.c, "session", self.sid, True)
            artifacts.scan(self.c, self.sid)
        self.assertTrue(self.c.execute("select pinned_at from artifacts where id=?", (report,)).fetchone()[0])
        cx = FakeCodex(TMP / f"codex-pin-{time.time_ns()}")
        cx.thread(self.sid, "pinned in the tracker")
        Reconciler(CodexSource(cx.state, cx.history)).run_once(self.c)
        self.assertTrue(self.c.execute("select pinned_at from sessions where id=?", (self.sid,)).fetchone()[0],
                        "syncing the thread from Codex keeps the pin")

class ArtifactCleanupTests(unittest.TestCase):
    def test_deleted_files_leave_search_and_unlinked_rows_go(self):
        c = fresh_db()
        sid = "ARTCLEAN"
        d = Path(config.ARTIFACT_ROOT) / "2026" / "10" / "05" / sid
        d.mkdir(parents=True)
        (d / "kept-link.md").write_text("zebrafish notes")
        (d / "orphan.md").write_text("zebrafish draft")
        with db.write(c):
            c.execute("insert into sessions(id, title, created_at, updated_at) values(?, 'a', 1, 1)", (sid,))
            artifacts.scan(c, sid)
            ids = {r["title"]: r["id"] for r in c.execute("select id, title from artifacts where session_id=?", (sid,))}
            t = store.create_todo(c, sid, {"title": "uses the note"})
            store.add_attachment(c, t["id"], {"kind": "artifact", "artifact_id": ids["kept-link.md"]})
        self.assertEqual(len(store.search(c, "zebrafish", kinds=["artifact"])), 2)
        (d / "kept-link.md").unlink()
        (d / "orphan.md").unlink()
        with db.write(c):
            artifacts.scan(c, sid)
        self.assertEqual(store.search(c, "zebrafish", kinds=["artifact"]), [])
        rows = {r["id"]: r["missing"] for r in c.execute("select id, missing from artifacts where session_id=?", (sid,))}
        self.assertEqual(rows, {ids["kept-link.md"]: 1})


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.c = fresh_db()
        with db.write(self.c):
            self.c.execute("insert into sessions(id, title, created_at, updated_at) values('S','Session',1,1)")

    def test_todo_lifecycle_validation_and_search(self):
        with db.write(self.c):
            t = store.create_todo(self.c, "S", {"title": "Ship the canvas link", "notes": "Use **bold** and :tada: with zanzibar", "priority": "P1"})
        self.assertEqual((t["state"], t["priority"]), ("not_started", "P1"))
        for bad in ({"state": "later"}, {"priority": "P9"}):
            with self.assertRaises(ValueError), db.write(self.c):
                store.update_todo(self.c, t["id"], bad)
        with db.write(self.c):
            t2 = store.update_todo(self.c, t["id"], {"state": "blocked"})
        self.assertEqual(t2["state"], "blocked")
        self.assertGreaterEqual(t2["updated_at"], t["updated_at"])
        hits = store.search(self.c, "zanzib")
        self.assertEqual([(h["kind"], h["ref_id"]) for h in hits], [("todo", str(t["id"]))])
        with self.assertRaises(ValueError), db.write(self.c):
            store.add_attachment(self.c, t["id"], {"kind": "url", "url": "javascript:alert(1)"})
        with db.write(self.c):
            store.add_attachment(self.c, t["id"], {"kind": "url", "url": "https://example.com/doc"})
        self.assertEqual(len(store.get_todo(self.c, t["id"])["attachments"]), 1)

    def test_ledger_edit_rules(self):
        with db.write(self.c):
            d = store.create_ledger(self.c, "S", {"kind": "decision", "title": "Use SQLite", "rationale": "fast"})
            m = store.create_ledger(self.c, "S", {"kind": "mistake", "title": "Assumed PATH", "body": "x", "lesson": "Use absolute paths."})
            i = store.create_ledger(self.c, "S", {"kind": "issue", "title": "Broken build"})
        with db.write(self.c):
            d2 = store.update_ledger(self.c, d["id"], {"title": "Use SQLite WAL", "rationale": "fast + durable", "alternatives": "Postgres · JSON files", "status": "decided"})
        self.assertEqual((d2["title"], d2["status"], d2["alternatives"]), ("Use SQLite WAL", "decided", ["Postgres", "JSON files"]))
        for bad in ({"title": "rewritten"}, {"status": "recorded"}, {"lesson": "new"}):
            with self.assertRaisesRegex(ValueError, "permanent"), db.write(self.c):
                store.update_ledger(self.c, m["id"], bad)
        self.assertEqual(store.get_ledger(self.c, m["id"])["title"], "Assumed PATH")
        with self.assertRaisesRegex(ValueError, "can't change: rationale"), db.write(self.c):
            store.update_ledger(self.c, i["id"], {"rationale": "x"})
        with self.assertRaisesRegex(ValueError, "Only you"), db.write(self.c):
            store.delete_ledger(self.c, m["id"], actor="agent")
        with db.write(self.c):
            store.delete_ledger(self.c, m["id"], actor="user")
        self.assertEqual(self.c.execute("select count(*) from ledger where kind='mistake'").fetchone()[0], 0)

    def test_corrections_keep_original_and_change_what_readers_see(self):
        with db.write(self.c):
            m = store.create_ledger(self.c, "S", {"kind": "mistake", "title": "Hash script failed", "rationale": "PATH lacked shasum",
                                                   "lesson": "Use absolute paths."})
            d = store.create_ledger(self.c, "S", {"kind": "decision", "title": "Use SQLite"})
        with self.assertRaisesRegex(ValueError, "Corrections are for mistakes"), db.write(self.c):
            store.create_correction(self.c, d["id"], {"lesson": "x"})
        with self.assertRaisesRegex(ValueError, "needs a corrected cause or lesson"), db.write(self.c):
            store.create_correction(self.c, m["id"], {"note": "only a note"})
        with self.assertRaisesRegex(ValueError, "source"), db.write(self.c):
            store.create_correction(self.c, m["id"], {"lesson": "x"}, source="agent")
        with db.write(self.c):
            store.create_correction(self.c, m["id"], {"cause": "the loop variable named path overwrote PATH in zsh",
                                                      "lesson": "Never name a zsh variable path.", "note": "re-check"}, source="scribe")
        e = store.get_ledger(self.c, m["id"])
        self.assertEqual((e["title"], e["rationale"], e["lesson"]), ("Hash script failed", "PATH lacked shasum", "Use absolute paths."))
        self.assertEqual((e["effective_cause"], e["effective_lesson"]), ("the loop variable named path overwrote PATH in zsh", "Never name a zsh variable path."))
        text = digest.build(self.c, "S", "SessionStart", "startup")
        self.assertIn("Never name a zsh variable path. [corrected]", text)
        self.assertNotIn("Use absolute paths.", text)
        self.assertEqual([h["ref_id"] for h in store.search(self.c, "overwrote")], [str(m["id"])])
        with self.assertRaises(ValueError), db.write(self.c):
            store.update_ledger(self.c, m["id"], {"lesson": "edited"})
        with db.write(self.c):
            store.delete_ledger(self.c, m["id"], actor="user")
        self.assertEqual(self.c.execute("select count(*) from corrections").fetchone()[0], 0)

    def test_feedback_labels(self):
        with db.write(self.c):
            self.c.execute("insert into turns(session_id, turn_id, ordinal, status) values('S', 'T1', 1, 'completed')")
            e = store.create_ledger(self.c, "S", {"kind": "mistake", "title": "Bogus", "turn_id": "T1"}, source="extractor")
            store.create_feedback(self.c, {"kind": "wrong", "ledger_id": e["id"], "note": "not a mistake"})
        self.assertEqual(store.get_ledger(self.c, e["id"])["flagged_wrong"], 1)
        for bad in ({"kind": "missed", "session_id": "S", "turn_id": "nope", "note": "x"},
                    {"kind": "missed", "session_id": "S", "turn_id": "T1", "note": ""}, {"kind": "other"}):
            with self.assertRaises(ValueError), db.write(self.c):
                store.create_feedback(self.c, bad)
        with db.write(self.c):
            store.create_feedback(self.c, {"kind": "missed", "session_id": "S", "turn_id": "T1", "note": "It skipped the tests."})
        self.assertEqual([f["kind"] for f in store.list_feedback(self.c, "S")], ["missed", "wrong"])

    def test_digest_is_bounded(self):
        with db.write(self.c):
            for i in range(300):
                store.create_todo(self.c, "S", {"title": f"todo number {i} " + "x" * 80})
                store.create_ledger(self.c, "S", {"kind": "mistake", "title": f"tried thing {i}", "body": "y" * 100})
        text = digest.build(self.c, "S", "SessionStart", "compact")
        self.assertLessEqual(len(text), config.DIGEST_MAX_CHARS)
        self.assertIn("more via MCP", text)
        self.assertIn("Open todos", text)
        self.assertIn("Lessons from mistakes", text)
        reload = hook.RESTORED + digest.build(self.c, "S", "SessionStart", "compact", reserve=len(hook.RESTORED))
        self.assertLessEqual(len(reload), config.DIGEST_MAX_CHARS, "the post-compaction reload prefix counts against the limit")
        self.assertTrue(reload.endswith(digest.RULES))


class OrderAndPinTests(unittest.TestCase):
    """Todos: priority, then the user's manual order. Pins: pinned sessions and artifacts are listed first."""

    def setUp(self):
        self.c = fresh_db()
        with db.write(self.c):
            for sid, parent, created in (("P", None, 1), ("K1", "P", 2), ("K2", "P", 3), ("Q", None, 4)):
                self.c.execute("insert into sessions(id, parent_id, root_id, depth, created_at, updated_at) values(?,?,?,?,?,?)",
                               (sid, parent, parent or sid, 1 if parent else 0, created, created))

    def order(self, sid="P"):
        return [(t["priority"], t["title"]) for t in store.todos_for(self.c, sid)]

    def test_todos_follow_priority_then_manual_order(self):
        with db.write(self.c):
            a = store.create_todo(self.c, "P", {"title": "a", "priority": "P2"})
            b = store.create_todo(self.c, "P", {"title": "b", "priority": "P1"})
            c = store.create_todo(self.c, "P", {"title": "c", "priority": "P2"})
            store.create_todo(self.c, "P", {"title": "d", "priority": "P2", "state": "done"})
        self.assertEqual(self.order(), [("P1", "b"), ("P2", "a"), ("P2", "c"), ("P2", "d")])
        with db.write(self.c):
            store.move_todo(self.c, c["id"], {"before": a["id"]})
            store.update_todo(self.c, a["id"], {"notes": "editing a todo does not reshuffle it"})
        self.assertEqual(self.order(), [("P1", "b"), ("P2", "c"), ("P2", "a"), ("P2", "d")])
        with db.write(self.c):
            moved = store.move_todo(self.c, a["id"], {"after": b["id"], "priority": "P1"})
        self.assertEqual(moved["priority"], "P1", "dropped into the P1 group, so it becomes P1")
        self.assertEqual(self.order(), [("P1", "b"), ("P1", "a"), ("P2", "c"), ("P2", "d")])
        with db.write(self.c):
            store.move_todo(self.c, a["id"], {"before": b["id"]})
            store.create_todo(self.c, "P", {"title": "e", "priority": "P1"})
        self.assertEqual(self.order()[:3], [("P1", "a"), ("P1", "b"), ("P1", "e")], "a new todo goes to the end of its priority")
        text = digest.build(self.c, "P")
        self.assertLess(text.index("] a (todo"), text.index("] b (todo"), "the digest shows the same order")
        with db.write(self.c):
            q = store.create_todo(self.c, "Q", {"title": "q"})
        for bad in ({"before": q["id"]}, {"after": a["id"]}, {"before": "x"}):
            with self.assertRaises(ValueError), db.write(self.c):
                store.move_todo(self.c, a["id"], bad)
        self.assertEqual(self.order()[0], ("P1", "a"), "a rejected move changes nothing")

    def test_existing_todos_keep_the_order_they_were_shown_in(self):
        with db.write(self.c):
            for title, prio, updated in (("old-p2", "P2", 5), ("new-p2", "P2", 9), ("p1", "P1", 1)):
                self.c.execute("insert into todos(session_id,title,priority,state,created_at,updated_at) values('P',?,?,'not_started',1,?)",
                               (title, prio, updated))
            db.backfill_todo_positions(self.c)
        self.assertEqual([t for _, t in self.order()], ["p1", "new-p2", "old-p2"])
        self.assertNotIn(None, [t["position"] for t in store.todos_for(self.c, "P")])

    def test_pinned_sessions_and_artifacts_come_first(self):
        with db.write(self.c):
            first = store.set_pinned(self.c, "session", "K2", True)["pinned_at"]
        self.assertEqual([ch["id"] for ch in store.session_view(self.c, "P", rescan=False)["children"]], ["K2", "K1"])
        p = next(n for n in store.tree(self.c) if n["id"] == "P")
        self.assertEqual([(ch["id"], bool(ch["pinned_at"])) for ch in p["children"]], [("K2", True), ("K1", False)])
        with db.write(self.c):
            self.assertEqual(store.set_pinned(self.c, "session", "K2", True)["pinned_at"], first, "re-pinning keeps its time")
            store.set_pinned(self.c, "session", "K2", False)
        self.assertEqual([ch["id"] for ch in store.session_view(self.c, "P", rescan=False)["children"]], ["K1", "K2"])
        with self.assertRaises(KeyError), db.write(self.c):
            store.set_pinned(self.c, "artifact", 999999, True)
        with self.assertRaises(KeyError), db.write(self.c):
            store.set_pinned(self.c, "session", "nope", True)
        with db.write(self.c):
            store.set_pinned(self.c, "session", "K1", True)
        self.assertTrue(mcp_server._compact_view(store.session_view(self.c, "P", rescan=False))["children"][0].get("pinned"))

    def test_digest_lists_pinned_children_first(self):
        with db.write(self.c):
            self.c.execute("insert into sessions(id, parent_id, root_id, depth, created_at, updated_at) values('K3','P','P',1,5,5)")
            store.set_pinned(self.c, "session", "K1", True)
        text = digest.build(self.c, "P")
        order = [line.split("(session ")[1].rstrip(")") for line in text.splitlines() if "(session " in line]
        self.assertEqual(order, ["K1", "K3", "K2"], "pinned first, then newest")
        self.assertIn("- [pinned] ", text)
        old_limit = config.DIGEST_MAX_CHARS
        config.DIGEST_MAX_CHARS = len(digest.RULES) + 520
        try:
            self.assertIn("(session K1)", digest.build(self.c, "P"), "a pinned child is the last to be cut")
        finally:
            config.DIGEST_MAX_CHARS = old_limit

class ScribeUpkeepTests(unittest.TestCase):
    """scribe-v7: the scribe keeps the task board, issues and artifacts current; digests carry the agent rules and
    what earlier sessions on the same repository learned."""

    def setUp(self):
        self.c = fresh_db()
        self.folder = TMP / f"art-{time.time_ns()}"
        self.folder.mkdir()
        with db.write(self.c):
            for sid, parent, origin, cwd in (("S", None, "https://github.com/o/repo.git", "/w/repo"), ("C1", "S", None, "/w/repo"),
                                              ("OLD", None, "https://github.com/o/repo.git", "/w/repo-wt"), ("OTHER", None, "https://github.com/o/x.git", "/w/x"),
                                              ("T1", None, None, "/tmp/st-e2e-work"), ("T2", None, None, "/tmp/st-e2e-work")):
                self.c.execute("""insert into sessions(id, parent_id, root_id, depth, git_origin_url, cwd, created_at, updated_at, artifacts_dir, status, task, result, agent_path)
                                  values(?,?,?,?,?,?,1,1,?,?,?,?,?)""",
                               (sid, parent, parent or sid, 1 if parent else 0, origin, cwd, str(self.folder) if sid == "S" else None,
                                "done" if sid == "C1" else "running", "Write the parser" if sid == "C1" else None,
                                "C1 RESULT parser done in parse.py" if sid == "C1" else None, "/root/c1" if sid == "C1" else None))

    def apply(self, result):
        with db.write(self.c):
            return extractor.apply_result(self.c, "S", "turn-1", result, "m")

    def test_board_upkeep_notes_drop_and_artifacts(self):
        counts = self.apply({"todos": [{"title": "Write the parser", "owner": "agent", "priority": "P1", "state": "waiting", "note": "delegated to /root/c1", "evidence": ["a1"]},
                                       {"title": "Ship the docs", "priority": "P2"}]})
        self.assertEqual(counts["todo"], 2)
        todos = {t["title"]: t for t in store.todos_for(self.c, "S")}
        parser = todos["Write the parser"]
        self.assertEqual(parser["state"], "waiting")
        self.assertIn("delegated to /root/c1", parser["notes"])
        ctx = extractor.context_block(self.c, "S")
        self.assertIn("/root/c1 [done] task: Write the parser — result: C1 RESULT parser done in parse.py", ctx)
        self.assertIn(f"Artifacts folder: {self.folder}", ctx)
        self.assertIn("last note: ", ctx)
        report = TMP / f"report-{time.time_ns()}.html"
        report.write_text("<h1>r</h1>")
        (self.folder / "inside.md").write_text("x")
        with db.write(self.c):
            issue = store.create_ledger(self.c, "S", {"kind": "issue", "title": "Build fails with ENOSPC", "body": "symptom: ENOSPC", "status": "open"})
        counts = self.apply({"updates": [{"todo_id": parser["id"], "state": "done", "note": "C1 finished: parse.py"},
                                         {"todo_id": parser["id"], "note": "C1 finished: parse.py"},
                                         {"ledger_id": issue["id"], "status": "resolved", "note": "cause: /tmp full; fix: clean cache"}],
                             "drop": [{"todo_id": todos["Ship the docs"]["id"], "reason": "user: docs not needed"}],
                             "artifacts": [{"path": str(report), "title": "Parser report"}, {"path": str(self.folder / "inside.md")},
                                           {"path": "/nope/missing.html"}, {"path": str(Path.home() / "Documents" / "x.html")}]})
        self.assertEqual((counts["update"], counts["drop"], counts["artifact"]), (2, 1, 1))
        parser = store.get_todo(self.c, parser["id"])
        self.assertEqual(parser["state"], "done")
        self.assertEqual(parser["notes"].count("C1 finished: parse.py"), 1, "a repeated note is not appended twice")
        self.assertEqual([t["title"] for t in store.todos_for(self.c, "S")], ["Write the parser"])
        ev = json.loads(self.c.execute("select payload from events where type='todo.deleted' order by id desc").fetchone()[0])
        self.assertEqual((ev["title"], ev["reason"]), ("Ship the docs", "user: docs not needed"))
        self.assertIn("notes", ev, "a deleted todo is kept whole in the event log")
        e = store.get_ledger(self.c, issue["id"])
        self.assertEqual(e["status"], "resolved")
        self.assertIn("cause: /tmp full; fix: clean cache", e["body"])
        arts = [a["path"] for a in self.c.execute("select path from artifacts where session_id='S'")]
        self.assertEqual(arts, [str(report)], "outside-folder deliverable linked; folder, missing and protected paths skipped")

    def test_rules_and_project_memory_in_digests(self):
        with db.write(self.c):
            store.create_ledger(self.c, "OLD", {"kind": "issue", "title": "Build cache misses on CI", "status": "open"})
            store.create_ledger(self.c, "OLD", {"kind": "mistake", "title": "Pushed without running tests", "status": "recorded",
                                                "lesson": "Run the test suite before every push."})
            store.create_ledger(self.c, "OTHER", {"kind": "issue", "title": "Unrelated repo issue", "status": "open"})
            store.create_ledger(self.c, "C1", {"kind": "issue", "title": "Own subtree issue", "status": "open"})
            store.create_ledger(self.c, "T1", {"kind": "issue", "title": "Temp folder issue", "status": "open"})
        text = digest.build(self.c, "S")
        self.assertIn(digest.RULES, text)
        self.assertIn("Open issues from earlier sessions in this project:", text)
        self.assertIn("Build cache misses on CI", text)
        self.assertIn("Run the test suite before every push.", text)
        for other in ("Unrelated repo issue", "Own subtree issue"):
            self.assertNotIn(other, text)
        self.assertNotIn("Temp folder issue", digest.build(self.c, "T2"), "temporary folders do not share memory")
        self.assertIn(digest.RULES, digest.build(self.c, "C1", "SubagentStart", None, "S"))

    def test_rules_survive_a_full_digest_and_scribe_json_with_a_stray_fence(self):
        with db.write(self.c):
            for i in range(80):
                store.create_todo(self.c, "S", {"title": f"task {i} " + "x" * 120})
        old = config.DIGEST_MAX_CHARS
        config.DIGEST_MAX_CHARS = 2000
        try:
            text = digest.build(self.c, "S")
        finally:
            config.DIGEST_MAX_CHARS = old
        self.assertTrue(text.endswith(digest.RULES) and len(text) <= 2000, "state is trimmed, the rules are not")
        self.assertEqual(extractor.parse_json('{"todos": []}\n\x60\x60\x60'), {"todos": []})
        self.assertEqual(extractor.parse_json('Here it is: {"todos": []} done'), {"todos": []})

class InstallAndCopilotTests(unittest.TestCase):
    """Out-of-the-box setup: the skill link, settings kept across re-installs, one service per checkout, Copilot discovery."""

    def setUp(self):
        from tracker import install
        self.install = install

    def test_skill_is_linked_once_and_an_existing_skill_is_kept(self):
        dst = self.install.skill_path()
        if dst.is_symlink() or dst.exists():
            dst.unlink()
        self.assertEqual(self.install.install_skill(), "linked")
        self.assertEqual(dst.resolve(), config.SKILL_SRC.resolve())
        self.assertEqual(self.install.install_skill(), "linked", "re-running is a no-op")
        self.install.uninstall_skill()
        self.assertFalse(dst.is_symlink())
        dst.mkdir(parents=True)
        (dst / "SKILL.md").write_text("someone else's skill")
        self.assertIn("kept the existing", self.install.install_skill())
        self.install.uninstall_skill()
        self.assertTrue((dst / "SKILL.md").exists(), "uninstall only removes its own link")
        (dst / "SKILL.md").unlink()
        dst.rmdir()

    def test_service_settings_survive_a_reinstall_without_them(self):
        prev = {"ST_EXTRACT_MODEL": "claude-opus-5.5", "ST_GH_USER": "me", "PATH": "/bin"}
        with unittest.mock.patch.dict(os.environ, {"ST_EXTRACT_EFFORT": "medium"}, clear=True):
            got = self.install._settings(prev)
        self.assertEqual(got, {"ST_EXTRACT_MODEL": "claude-opus-5.5", "ST_GH_USER": "me", "ST_EXTRACT_EFFORT": "medium"})
        with unittest.mock.patch.dict(os.environ, {"ST_EXTRACT_MODEL": "claude-sonnet-5.5"}, clear=True):
            self.assertEqual(self.install._settings(prev)["ST_EXTRACT_MODEL"], "claude-sonnet-5.5", "the current environment wins")

    def test_an_install_under_another_label_is_retired(self):
        import plistlib
        agents = TMP / f"agents-{time.time_ns()}"
        agents.mkdir()
        mine = {"Label": "old.label.for.test", "ProgramArguments": ["/usr/bin/python3", "-m", "tracker", "serve"],
                "WorkingDirectory": str(config.PROJECT_DIR)}
        other = {"Label": "unrelated", "ProgramArguments": ["/usr/bin/python3", "-m", "tracker", "serve"], "WorkingDirectory": "/elsewhere"}
        (agents / "old.plist").write_bytes(plistlib.dumps(mine))
        (agents / "other.plist").write_bytes(plistlib.dumps(other))
        with unittest.mock.patch.object(self.install, "AGENTS_DIR", agents):
            self.assertEqual([d["Label"] for _, d in self.install._service_agents()], ["old.label.for.test"])
            self.assertEqual(self.install._retire_other_labels(), ["old.label.for.test"])
        self.assertFalse((agents / "old.plist").exists())
        self.assertTrue((agents / "other.plist").exists(), "another checkout's service is left alone")

    def test_copilot_api_host_comes_from_the_account_unless_set(self):
        c = extractor.Copilot()
        with unittest.mock.patch.object(config, "COPILOT_BASE", ""), \
                unittest.mock.patch.object(c, "account", return_value={"endpoints": {"api": "https://api.business.githubcopilot.com/"}}):
            self.assertEqual(c.base(), "https://api.business.githubcopilot.com")
        c = extractor.Copilot()
        with unittest.mock.patch.object(config, "COPILOT_BASE", ""), \
                unittest.mock.patch.object(c, "account", side_effect=extractor.ExtractError("no copilot")):
            self.assertEqual(c.base(), extractor.DEFAULT_COPILOT_BASE)
        with unittest.mock.patch.object(config, "COPILOT_BASE", "https://proxy.example"):
            self.assertEqual(extractor.Copilot().base(), "https://proxy.example")

    def test_only_models_the_scribe_can_call_count_as_available(self):
        data = {"data": [
            {"id": "claude-sonnet-5.5", "capabilities": {"type": "chat"}, "policy": {"state": "enabled"}, "supported_endpoints": ["/chat/completions"]},
            {"id": "gpt-6.1-sol", "capabilities": {"type": "chat"}, "policy": {"state": "enabled"}, "supported_endpoints": ["/responses"]},
            {"id": "claude-opus-5.5", "capabilities": {"type": "chat"}, "policy": {"state": "disabled"}},
            {"id": "text-embedding", "capabilities": {"type": "embeddings"}},
            {"id": "older-chat", "capabilities": {"type": "chat"}}]}
        c = extractor.Copilot()
        with unittest.mock.patch.object(c, "_request", return_value=data):
            self.assertEqual(c.chat_models(), ["claude-sonnet-5.5", "older-chat"])

    def test_unreachable_service_error_says_how_to_restart_it(self):
        with unittest.mock.patch.object(config, "BASE_URL", "http://127.0.0.1:9"):
            with self.assertRaises(mcp_server.ToolError) as ctx:
                mcp_server.api("GET", "/api/health")
        self.assertIn(f"launchctl kickstart -k gui/{os.getuid()}/{config.LABEL}", str(ctx.exception))
        self.assertIn("install.sh", str(ctx.exception))


class ExtractorTests(unittest.TestCase):
    def setUp(self):
        self.c = fresh_db()
        self.codex = FakeCodex(TMP / f"codex-x-{time.time_ns()}")
        self.codex.thread("S", "Session")
        self.codex.turn("S", "t1", "completed", NOW)
        self.codex.item("S", "t1", "aaaaaaaa11111111", "userMessage", {"content": [{"type": "text", "text": "use sqlite; token=supersecretvalue123"}]})
        self.codex.item("S", "t1", "bbbbbbbb22222222", "commandExecution", {"command": "pytest", "exitCode": 1, "status": "failed", "aggregatedOutput": "boom"})
        Reconciler(CodexSource(self.codex.state, self.codex.history)).run_once(self.c)
        with db.write(self.c):
            self.old_issue = store.create_ledger(self.c, "S", {"kind": "issue", "title": "Tests red", "status": "open"})

    def test_apply_dedupe_updates_and_redaction(self):
        payload = {"decisions": [{"title": "Use SQLite", "choice": "SQLite WAL", "rationale": "local + fast", "alternatives_rejected": ["Postgres"],
                                  "made_by": "user", "status": "decided", "evidence": ["11111111"]}],
                   "mistakes": [{"title": "Ran pytest before creating fixtures", "what_happened": "pytest failed: boom",
                                 "why_it_was_a_mistake": "assumed fixtures existed", "lesson": "Create fixtures before running pytest.",
                                 "repeat_of": None, "evidence": ["22222222"]}],
                   "issues": [], "todos": [{"title": "Add fixtures", "owner": "agent", "priority": "P1", "evidence": []}],
                   "updates": [{"ledger_id": self.old_issue["id"], "status": "resolved"}, {"ledger_id": 999999, "status": "resolved"}]}
        client = FakeClient(payload)
        w = extractor.ExtractorWorker(CodexSource(self.codex.state, self.codex.history), client)
        counts = w.process(self.c, "S", "t1")
        self.assertEqual(counts, {"decision": 1, "mistake": 1, "correction": 0, "issue": 0, "todo": 1, "update": 1, "drop": 0, "artifact": 0})
        self.assertNotIn("supersecretvalue123", client.calls[0])
        self.assertIn("#" + str(self.old_issue["id"]) + " issue [open] Tests red", client.calls[0])
        self.assertEqual(store.get_ledger(self.c, self.old_issue["id"])["status"], "resolved")
        m = self.c.execute("select title, body, rationale, lesson, source from ledger where kind='mistake'").fetchone()
        self.assertEqual(tuple(m), ("Ran pytest before creating fixtures", "pytest failed: boom", "assumed fixtures existed",
                                    "Create fixtures before running pytest.", "extractor"))
        w.process(self.c, "S", "t1")
        self.assertEqual(self.c.execute("select count(*) from ledger where source='extractor'").fetchone()[0], 2)
        self.assertEqual(self.c.execute("select count(*) from todos").fetchone()[0], 1)
        self.assertEqual(self.c.execute("select extract_status from turns where turn_id='t1'").fetchone()[0], "done")

    def test_repeated_mistake_is_recorded_again_and_mistakes_are_never_updated(self):
        first = {"mistakes": [{"title": "Skipped the tests", "what_happened": "x", "why_it_was_a_mistake": "y", "lesson": "Run the tests.", "evidence": []}]}
        w = extractor.ExtractorWorker(CodexSource(self.codex.state, self.codex.history), FakeClient(first))
        w.process(self.c, "S", "t1")
        mid = self.c.execute("select id from ledger where kind='mistake'").fetchone()[0]
        again = {"mistakes": [{"title": "Skipped the tests", "what_happened": "again", "why_it_was_a_mistake": "y",
                               "lesson": "Always run the tests before claiming done.", "repeat_of": mid, "evidence": []}],
                 "updates": [{"ledger_id": mid, "status": "resolved"}]}
        w.client = FakeClient(again)
        w.process(self.c, "S", "t1")
        rows = self.c.execute("select id, status, lesson from ledger where kind='mistake' order by id").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["status"], "recorded")
        self.assertIn("Repeated mistake", rows[1]["lesson"])

    def test_repeat_of_must_be_an_existing_mistake_in_this_session(self):
        with db.write(self.c):
            self.c.execute("insert into sessions(id, title, created_at, updated_at) values('OTHER','Other',1,1)")
            foreign = store.create_ledger(self.c, "OTHER", {"kind": "mistake", "title": "Foreign mistake", "lesson": "x"})
            own_decision = store.create_ledger(self.c, "S", {"kind": "decision", "title": "Not a mistake"})
        payload = {"mistakes": [{"title": f"Mistake {i}", "what_happened": "x", "why_it_was_a_mistake": "y", "lesson": "Do better.",
                                 "repeat_of": ref, "evidence": []}
                                for i, ref in enumerate([foreign["id"], own_decision["id"], 999999, "abc"])]}
        w = extractor.ExtractorWorker(CodexSource(self.codex.state, self.codex.history), FakeClient(payload))
        self.assertEqual(w.process(self.c, "S", "t1")["mistake"], 4)
        lessons = [r[0] for r in self.c.execute("select lesson from ledger where session_id='S' and kind='mistake'")]
        self.assertEqual(lessons, ["Do better."] * 4)
        first = self.c.execute("select id from ledger where session_id='S' and kind='mistake' order by id").fetchone()[0]
        w.client = FakeClient({"mistakes": [{"title": "Mistake 0", "what_happened": "again", "lesson": "", "repeat_of": 999999, "evidence": []}]})
        self.assertEqual(w.process(self.c, "S", "t1")["mistake"], 1)
        last = self.c.execute("select lesson from ledger where session_id='S' and kind='mistake' order by id desc").fetchone()[0]
        self.assertEqual(last, f"(Repeated mistake — see #{first}.)")

    def test_scribe_corrections_only_for_own_mistakes_and_prompt_has_facts(self):
        with db.write(self.c):
            self.c.execute("insert into sessions(id, title, created_at, updated_at) values('OTHER2','Other',1,1)")
            mine = store.create_ledger(self.c, "S", {"kind": "mistake", "title": "Wrong cause", "rationale": "symptom"}, source="extractor")
            theirs = store.create_ledger(self.c, "OTHER2", {"kind": "mistake", "title": "Foreign"}, source="extractor")
        client = FakeClient({"corrections": [{"mistake_id": mine["id"], "cause": "real cause", "lesson": "Check X first.", "why": "found later"},
                                             {"mistake_id": theirs["id"], "lesson": "nope"}, {"mistake_id": "x", "lesson": "nope"}]})
        w = extractor.ExtractorWorker(CodexSource(self.codex.state, self.codex.history), client)
        self.assertEqual(w.process(self.c, "S", "t1")["correction"], 1)
        e = store.get_ledger(self.c, mine["id"])
        self.assertEqual((e["effective_lesson"], e["corrections"][0]["source"]), ("Check X first.", "scribe"))
        self.assertEqual(store.get_ledger(self.c, theirs["id"])["corrections"], [])
        prompt = client.calls[0]
        self.assertIn("computed from Codex's records", prompt)
        self.assertIn("Commands run: 1 (1 exited non-zero)", prompt)
        self.assertIn("Tests, builds, linters or type checks run (1): pytest → exit 1", prompt)
        self.assertIn(f"#{mine['id']} mistake [recorded] Wrong cause — cause: real cause — lesson: Check X first.", extractor.context_block(self.c, "S"))

    def test_recheck_appends_correction_or_confirms(self):
        with db.write(self.c):
            m = store.create_ledger(self.c, "S", {"kind": "mistake", "title": "No lesson", "turn_id": "t1"}, source="extractor")
            d = store.create_ledger(self.c, "S", {"kind": "decision", "title": "x"})
        src = CodexSource(self.codex.state, self.codex.history)
        out = extractor.recheck_mistake(self.c, FakeClient({"verdict": "corrected", "cause": "c", "lesson": "Do Y before Z."}), src, m["id"])
        self.assertEqual((out["verdict"], out["correction"]["lesson"]), ("corrected", "Do Y before Z."))
        out = extractor.recheck_mistake(self.c, FakeClient({"verdict": "confirmed"}), src, m["id"])
        self.assertEqual(out["verdict"], "confirmed")
        self.assertEqual(len(store.get_ledger(self.c, m["id"])["corrections"]), 1)
        with self.assertRaisesRegex(ValueError, "only mistakes"):
            extractor.recheck_mistake(self.c, FakeClient({}), src, d["id"])

    def test_old_prompt_shape_still_accepted(self):
        old = {"failed_attempts": [{"attempt": "tried X", "why_it_failed": "Y", "what_worked_instead": "Z", "evidence": []}]}
        w = extractor.ExtractorWorker(CodexSource(self.codex.state, self.codex.history), FakeClient(old))
        self.assertEqual(w.process(self.c, "S", "t1")["mistake"], 1)

    def test_failure_is_recorded(self):
        class Broken(FakeClient):
            def chat(self, *a):
                raise extractor.ExtractError("Copilot HTTP 500: nope")
        w = extractor.ExtractorWorker(CodexSource(self.codex.state, self.codex.history), Broken({}))
        self.assertIsNone(w.process(self.c, "S", "t1"))
        row = self.c.execute("select extract_status, extract_error from turns where turn_id='t1'").fetchone()
        self.assertEqual(row[0], "failed")
        self.assertIn("500", row[1])

    def test_redaction_patterns(self):
        samples = ["github_pat_11ABCDEFG0123456789_abcdefghijklmnopqrstuvwxyz", "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9abcdefgh",
                   "AIzaSyA1234567890abcdefghijklmnopqrstu", "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA\n-----END OPENSSH PRIVATE KEY-----",
                   "sk-proj-abcdefghijklmnop1234", "ghp_abcdefghijklmnopqrstuvwxyz0123", "https://x.io/mcp?key=0123456789abcdef0123"]
        for s in samples:
            out = delta.redact("before " + s + " after")
            self.assertIn("[REDACTED]", out, s)
            self.assertNotIn(s[12:24], out, s)
        self.assertEqual(delta.redact("plain text with a token word"), "plain text with a token word")

    def test_chunking_and_fences(self):
        self.assertEqual(len(delta.chunk(["a" * 50] * 10, 120)), 5)
        self.assertEqual(extractor.parse_json("\x60\x60\x60json\n{\"decisions\": []}\n\x60\x60\x60"), {"decisions": []})


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = fresh_db()
        cls.revealed = []
        cls.server = api.Server(("127.0.0.1", 0), reveal=cls.revealed.append)
        cls.port = cls.server.server_port
        config.BASE_URL = f"http://127.0.0.1:{cls.port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        sid = "HTTPS1"
        cls.sid = sid
        d = Path(config.ARTIFACT_ROOT) / "2026" / "10" / "04" / sid
        d.mkdir(parents=True)
        (d / "page.html").write_text("<script>1</script><img src='img.png'>")
        (d / "img.png").write_bytes(b"\x89PNG")
        with db.write(cls.c):
            cls.c.execute("insert into sessions(id, title, created_at, updated_at) values(?,?,?,?)", (sid, "HTTP session", db.now_ms(), db.now_ms()))
            cls.c.execute("insert into sessions(id, parent_id, root_id, depth, title, created_at, updated_at) values('HTTPC', ?, ?, 1, 'kid', 1, 1)", (sid, sid))

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def req(self, method, path, body=None, headers=None, raw=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        h = {"Content-Type": "application/json"} if body is not None else {}
        h.update(headers or {})
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def test_session_view_todo_reference_raw_and_reveal(self):
        code, _, body = self.req("POST", f"/api/sessions/{self.sid}/todos", {"title": "Review the spec 📄", "priority": "P1"})
        self.assertEqual(code, 200, body)
        todo = json.loads(body)
        code, _, body = self.req("POST", f"/api/sessions/{self.sid}/references?todo={todo['id']}", raw=b"hello",
                                 headers={"X-Filename": "brief%20v1.txt", "Content-Type": "text/plain"})
        self.assertEqual(code, 200, body)
        ref = json.loads(body)["artifact"]
        self.assertTrue(ref["path"].endswith("/HTTPS1/references/brief v1.txt"))
        code, _, body = self.req("GET", f"/api/sessions/{self.sid}")
        view = json.loads(body)
        self.assertEqual(view["todos"][0]["attachments"][0]["kind"], "reference")
        self.assertEqual([ch["id"] for ch in view["children"]], ["HTTPC"])
        page = next(a for a in view["artifacts"] if a["title"] == "page.html")
        code, headers, body = self.req("GET", f"/raw/{page['id']}/page.html")
        self.assertEqual(code, 200)
        self.assertIn("sandbox", headers.get("Content-Security-Policy", ""))
        code, headers, _ = self.req("GET", f"/raw/{page['id']}/img.png")
        self.assertEqual((code, headers.get("Content-Security-Policy")), (200, None))
        code, _, _ = self.req("GET", f"/raw/{page['id']}/..%2F..%2F..%2Fetc%2Fpasswd")
        self.assertEqual(code, 404)
        outside = Path(tempfile.mkdtemp(prefix="st-linked-"))
        (outside / "linked.md").write_text("# linked")
        (outside / "secret.txt").write_text("do not serve")
        code, _, body = self.req("POST", f"/api/sessions/{self.sid}/artifacts", {"path": str(outside / "linked.md")})
        linked = json.loads(body)
        self.assertEqual(self.req("GET", f"/raw/{linked['id']}/linked.md")[0], 200)
        self.assertEqual(self.req("GET", f"/raw/{linked['id']}/secret.txt")[0], 404)
        code, _, body = self.req("POST", f"/api/artifacts/{page['id']}/reveal")
        self.assertEqual(code, 200, body)
        self.assertTrue(self.revealed[-1].endswith("page.html"))

    def test_requests_do_not_leak_file_descriptors(self):
        """Regression: each request thread's DB connection must be closed (launchd allows 256 open files)."""
        self.req("GET", "/api/health")
        time.sleep(0.2)
        before = len(os.listdir("/dev/fd"))
        for _ in range(60):
            self.assertEqual(self.req("GET", "/api/health")[0], 200)
        time.sleep(0.5)
        after = len(os.listdir("/dev/fd"))
        self.assertLess(after - before, 10, f"open fds grew from {before} to {after}")

    def test_guard_rejects_foreign_host_and_origin(self):
        self.assertEqual(self.req("GET", "/api/health", headers={"Host": "evil.example"})[0], 403)
        self.assertEqual(self.req("POST", f"/api/sessions/{self.sid}/todos", {"title": "x"}, headers={"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.req("POST", f"/api/sessions/{self.sid}/todos", {"title": "ok"}, headers={"Origin": f"http://127.0.0.1:{self.port}"})[0], 200)

    def test_artifact_origin_serves_only_files(self):
        view = json.loads(self.req("GET", f"/api/sessions/{self.sid}")[2])
        page = next(a for a in view["artifacts"] if a["title"] == "page.html")
        own = {"Host": f"a{page['id']}.localhost:{self.port}"}
        code, headers, _ = self.req("GET", f"/raw/{page['id']}/page.html", headers=own)
        self.assertEqual(code, 200)
        self.assertIn("allow-same-origin", headers["Content-Security-Policy"])
        self.assertEqual(self.req("GET", f"/raw/{page['id']}/img.png", headers=own)[0], 200)
        other = next(a for a in view["artifacts"] if a["id"] != page["id"] and a.get("path"))
        self.assertEqual(self.req("GET", f"/raw/{other['id']}/x", headers=own)[0], 403)
        self.assertEqual(self.req("GET", "/api/health", headers=own)[0], 403)
        self.assertEqual(self.req("POST", f"/api/sessions/{self.sid}/todos", {"title": "x"}, headers=own)[0], 403)
        code, headers, _ = self.req("GET", f"/raw/{page['id']}/page.html")
        self.assertNotIn("allow-same-origin", headers["Content-Security-Policy"])
        self.assertEqual(self.req("GET", "/api/health", headers={"Host": f"localhost:{self.port}"})[0], 403)
        self.assertEqual(self.req("POST", f"/api/sessions/{self.sid}/todos", {"title": "x"}, headers={"Host": f"localhost:{self.port}", "Origin": f"http://localhost:{self.port}"})[0], 403)
        opener = urllib.request.build_opener(type("NoRedirect", (urllib.request.HTTPRedirectHandler,), {"redirect_request": lambda *a: None}))

        def redirect(path):
            r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", headers={"Host": f"localhost:{self.port}"})
            try:
                opener.open(r, timeout=5)
                return 200, None
            except urllib.error.HTTPError as e:
                return e.code, e.headers.get("Location")
        self.assertEqual(redirect(f"/s/{self.sid}"), (302, f"http://127.0.0.1:{self.port}/s/{self.sid}"))
        self.assertEqual(redirect(f"/raw/{page['id']}/page.html"), (302, f"http://a{page['id']}.localhost:{self.port}/raw/{page['id']}/page.html"))

    def test_rejected_request_body_does_not_poison_keep_alive_connection(self):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        body = json.dumps({"lesson": "agent rewrite " + "x" * 200})
        conn.request("POST", "/api/ledger/1/corrections", body=body, headers={"Content-Type": "application/json"})
        r = conn.getresponse()
        self.assertEqual(r.status, 403)
        r.read()
        conn.request("GET", "/api/health")
        r = conn.getresponse()
        self.assertEqual((r.status, r.getheader("Content-Type").split(";")[0]), (200, "application/json"))
        self.assertTrue(json.loads(r.read())["ok"])
        conn.close()

    def test_corrections_and_feedback_are_user_only(self):
        code, _, body = self.req("POST", f"/api/sessions/{self.sid}/ledger", {"kind": "mistake", "title": "Http mistake"}, headers={"X-Actor": "user"})
        mid = json.loads(body)["id"]
        self.assertEqual(self.req("POST", f"/api/ledger/{mid}/corrections", {"lesson": "x"})[0], 403)
        self.assertEqual(self.req("POST", f"/api/ledger/{mid}/feedback", {"note": "x"})[0], 403)
        code, _, body = self.req("POST", f"/api/ledger/{mid}/corrections", {"lesson": "Fixed lesson"}, headers={"X-Actor": "user"})
        self.assertEqual((code, json.loads(body)["effective_lesson"]), (200, "Fixed lesson"))
        out = mcp_server.handle({"id": 30, "method": "tools/call", "params": {"name": "get_session", "arguments": {}, "_meta": {"threadId": self.sid}}})
        entry = next(e for e in json.loads(out["content"][0]["text"])["ledger"]["mistake"] if e["id"] == mid)
        self.assertEqual((entry["effective_lesson"], entry["corrections"][0]["lesson"]), ("Fixed lesson", "Fixed lesson"))
        self.assertEqual(self.req("POST", f"/api/ledger/{mid}/feedback", {"note": "wrong"}, headers={"X-Actor": "user"})[0], 200)
        self.assertEqual(json.loads(self.req("GET", f"/api/sessions/{self.sid}/turns")[2]), {"turns": []})

    def test_request_without_actor_header_acts_as_agent(self):
        with db.write(self.c):
            self.c.execute("insert or ignore into sessions(id, title, created_at, updated_at) values('NOHDR', 'no header', 1, 1)")
        code, _, body = self.req("POST", "/api/sessions/NOHDR/todos", {"title": "scripted"})
        self.assertEqual(json.loads(body)["created_by"], "agent")
        code, _, body = self.req("POST", "/api/sessions/NOHDR/ledger", {"kind": "mistake", "title": "No header", "lesson": "x"})
        self.assertEqual(code, 200, body)
        mid = json.loads(body)["id"]
        self.assertEqual(self.req("DELETE", f"/api/ledger/{mid}")[0], 400)
        self.assertEqual(self.req("DELETE", f"/api/ledger/{mid}", headers={"X-Actor": "user"})[0], 200)

    def test_agent_delete_todo_is_scoped_to_its_subtree(self):
        def todo(sid):
            return json.loads(self.req("POST", f"/api/sessions/{sid}/todos", {"title": f"todo of {sid}"})[2])["id"]
        with db.write(self.c):
            self.c.execute("insert or ignore into sessions(id, title, created_at, updated_at) values('DROOT', 'root', 1, 1)")
            for kid in ("DKID", "DSIB"):
                self.c.execute("insert or ignore into sessions(id, parent_id, root_id, depth, title, created_at, updated_at) values(?, 'DROOT', 'DROOT', 1, ?, 1, 1)", (kid, kid))
        parent_todo, sibling_todo = todo("DROOT"), todo("DSIB")
        as_child = {"X-Actor": "agent", "X-Caller-Session": "DKID"}
        self.assertEqual(self.req("DELETE", f"/api/todos/{parent_todo}", headers=as_child)[0], 403)
        self.assertEqual(self.req("DELETE", f"/api/todos/{sibling_todo}", headers=as_child)[0], 403)
        self.assertEqual(self.req("DELETE", f"/api/todos/{parent_todo}", headers={"X-Actor": "agent"})[0], 403)
        self.assertEqual(self.req("DELETE", f"/api/todos/{todo('DKID')}", headers=as_child)[0], 200)
        as_parent = {"X-Actor": "agent", "X-Caller-Session": "DROOT"}
        self.assertEqual(self.req("DELETE", f"/api/todos/{sibling_todo}", headers=as_parent)[0], 200)
        self.assertEqual(self.req("DELETE", f"/api/todos/{parent_todo}", headers=as_parent)[0], 200)
        self.assertEqual(self.req("DELETE", f"/api/todos/{todo('DKID')}", headers={"X-Actor": "user"})[0], 200)
        out = mcp_server.handle({"id": 20, "method": "tools/call", "params": {"name": "delete_todo", "arguments": {"todo_id": todo("DROOT")},
                                                                             "_meta": {"threadId": "DKID"}}})
        self.assertTrue(out["isError"])
        self.assertIn("403", out["content"][0]["text"])
        out = mcp_server.handle({"id": 21, "method": "tools/call", "params": {"name": "update_todo", "arguments": {}, "_meta": {"threadId": "DKID"}}})
        self.assertIn("missing required argument", out["content"][0]["text"])

    def test_mcp_identity_from_meta(self):
        meta = {"threadId": "HTTPC", "x-codex-turn-metadata": {"thread_id": "HTTPC", "parent_thread_id": self.sid}}
        out = mcp_server.handle({"id": 1, "method": "tools/call", "params": {"name": "add_todo", "arguments": {"title": "from child"}, "_meta": meta}})
        self.assertFalse(out.get("isError"), out)
        self.assertEqual(json.loads(out["content"][0]["text"])["session_id"], "HTTPC")
        out = mcp_server.handle({"id": 2, "method": "tools/call", "params": {"name": "record", "arguments": {"kind": "decision", "title": "use X"}, "_meta": meta}})
        self.assertEqual(json.loads(out["content"][0]["text"])["source"], "agent")
        out = mcp_server.handle({"id": 3, "method": "tools/call", "params": {"name": "get_tree", "arguments": {}, "_meta": meta}})
        tree = json.loads(out["content"][0]["text"])["tree"]
        self.assertEqual(tree[0]["id"], self.sid)
        self.assertEqual(tree[0]["children"][0]["id"], "HTTPC")
        todo_id = json.loads(mcp_server.handle({"id": 10, "method": "tools/call", "params": {"name": "add_todo", "arguments": {"title": "to delete"}, "_meta": meta}})["content"][0]["text"])["id"]
        out = mcp_server.handle({"id": 11, "method": "tools/call", "params": {"name": "delete_todo", "arguments": {"todo_id": todo_id}, "_meta": meta}})
        self.assertFalse(out.get("isError"), out)
        self.assertEqual(self.req("PATCH", f"/api/todos/{todo_id}", {"state": "done"})[0], 404)
        mistake_id = json.loads(mcp_server.handle({"id": 12, "method": "tools/call", "params": {"name": "record", "arguments": {
            "kind": "mistake", "title": "Assumed X", "lesson": "Check X first."}, "_meta": meta}})["content"][0]["text"])["id"]
        out = mcp_server.handle({"id": 13, "method": "tools/call", "params": {"name": "update_ledger", "arguments": {"entry_id": mistake_id, "title": "nope"}, "_meta": meta}})
        self.assertTrue(out["isError"])
        self.assertIn("permanent", out["content"][0]["text"])
        self.assertEqual(self.req("DELETE", f"/api/ledger/{mistake_id}", headers={"X-Actor": "agent"})[0], 400)
        view = mcp_server.handle({"id": 14, "method": "tools/call", "params": {"name": "get_session", "arguments": {}, "_meta": meta}})
        lessons = [e.get("lesson") for e in json.loads(view["content"][0]["text"])["ledger"]["mistake"]]
        self.assertIn("Check X first.", lessons)
        out = mcp_server.handle({"id": 4, "method": "tools/call", "params": {"name": "add_todo", "arguments": {"title": "no identity"}}})
        self.assertTrue(out["isError"])
        self.assertEqual({t["name"] for t in mcp_server.handle({"id": 5, "method": "tools/list"})["tools"]} >= {"get_session", "record", "add_todo"}, True)


class InstallTests(unittest.TestCase):
    def test_hooks_install_named_idempotent_and_uninstall_keeps_foreign(self):
        from tracker import install
        fresh_db()
        path = install.hooks_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        foreign = {"type": "command", "command": "/x/tab-color.sh"}
        path.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [foreign]}]}}))
        install.install_hooks()
        data = json.loads(path.read_text())
        ours = [h for ev in data["hooks"].values() for g in ev for h in g["hooks"] if install.HOOK_MARK in h["command"]]
        self.assertEqual(sorted(h["statusMessage"] for h in ours), sorted(install.HOOK_NAMES.values()))
        self.assertEqual(data["hooks"]["Stop"][0]["hooks"], [foreign])
        changed = db.get_meta("hooks_changed_at", None, db.conn())
        self.assertIsNotNone(changed)
        time.sleep(0.01)
        install.install_hooks()
        self.assertEqual(json.loads(path.read_text()), data, "re-installing must not change the definitions Codex trusted")
        self.assertEqual(db.get_meta("hooks_changed_at", None, db.conn()), changed)
        install.uninstall_hooks()
        self.assertEqual(json.loads(path.read_text()), {"hooks": {"Stop": [{"hooks": [foreign]}]}})
        install.install_hooks()
        self.assertEqual(db.get_meta("hooks_changed_at", None, db.conn()), changed, "reinstalling identical hooks after uninstall is not a change")
        saved = install.HOOK_NAMES["Stop"]
        install.HOOK_NAMES["Stop"] = "renamed"
        try:
            install.install_hooks()
        finally:
            install.HOOK_NAMES["Stop"] = saved
        self.assertNotEqual(db.get_meta("hooks_changed_at", None, db.conn()), changed)


class FeedbackExportTests(unittest.TestCase):
    def test_export_builds_a_gradable_eval_set(self):
        import importlib.util
        from tracker import feedback_export
        c = fresh_db()
        codex = FakeCodex(TMP / f"codex-fb-{time.time_ns()}")
        codex.thread("FB", "Session")
        codex.turn("FB", "ft1", "completed", NOW)
        codex.item("FB", "ft1", "item00000001", "userMessage", {"content": [{"type": "text", "text": "why did you skip the tests?"}]})
        src = CodexSource(codex.state, codex.history)
        Reconciler(src).run_once(c)
        with db.write(c):
            e = store.create_ledger(c, "FB", {"kind": "mistake", "title": "Invented problem", "turn_id": "ft1"}, source="extractor")
            store.create_feedback(c, {"kind": "wrong", "ledger_id": e["id"], "note": "not a mistake"})
            store.create_feedback(c, {"kind": "missed", "session_id": "FB", "turn_id": "ft1", "note": "Skipped the tests."})
        out = TMP / f"fb-export-{time.time_ns()}"
        self.assertEqual(feedback_export.export(out, c, src)["cases"], 1)
        manifest = json.loads((out / "manifest.json").read_text())["cases"]
        self.assertEqual(manifest["F01"]["turn_id"], "ft1")
        self.assertIn("why did you skip the tests?", (out / "F01.txt").read_text())
        spec = importlib.util.spec_from_file_location("judge_mod", Path(__file__).resolve().parents[1] / "eval" / "judge.py")
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "eval"))
        judge_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(judge_mod)
        pre, sections = judge_mod.split_key((out / "answer_key.md").read_text())
        self.assertEqual(judge_mod.item_ids("F01", sections["F01"]), ["F01.M1"])
        self.assertIn("Invented problem", sections["F01"])


class McpApprovalTests(unittest.TestCase):
    def test_tracker_tools_are_preapproved_once_and_other_tables_untouched(self):
        from tracker import install
        path = config.CODEX_HOME / "config.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        original = ('model = "x"\n\n[mcp_servers.session_tracker]\ncommand = "/py"\nargs = ["/st-mcp"]\n\n'
                    '[mcp_servers.other]\ncommand = "o"\n')
        path.write_text(original)
        self.assertTrue(install.approve_mcp_tools())
        self.assertFalse(install.approve_mcp_tools())
        import tomllib
        data = tomllib.loads(path.read_text())
        self.assertEqual(data["mcp_servers"]["session_tracker"]["default_tools_approval_mode"], "approve")
        self.assertNotIn("default_tools_approval_mode", data["mcp_servers"]["other"])
        self.assertEqual(data["model"], "x")


class HookTests(unittest.TestCase):
    def test_fallback_when_service_down_and_spool_ingest(self):
        saved = config.BASE_URL
        config.BASE_URL = "http://127.0.0.1:9"
        try:
            out = hook.respond({"hook_event_name": "SubagentStart", "session_id": "P1X", "agent_id": "C1X"})
        finally:
            config.BASE_URL = saved
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("C1X", ctx)
        self.assertIsNone(hook.respond({"hook_event_name": "Stop", "session_id": "P1X"}))
        c = fresh_db()
        spool.append({"payload": {"hook_event_name": "SessionStart", "session_id": "P1X", "source": "startup"}})
        spool.append({"payload": {"hook_event_name": "SubagentStart", "session_id": "P1X", "agent_id": "C1X"}})
        self.assertGreaterEqual(spool.ingest(c), 2)
        row = c.execute("select parent_id from sessions where id='C1X'").fetchone()
        self.assertEqual(row[0], "P1X")
        self.assertEqual(spool.ingest(c), 0)

    def test_compacted_subagent_gets_its_state_on_the_next_tool_call(self):
        from tracker import restore
        saved = config.BASE_URL
        config.BASE_URL = "http://127.0.0.1:9"
        try:
            self.assertIsNone(hook.respond({"hook_event_name": "PostCompact", "session_id": "PARENT1", "agent_id": "KID1", "trigger": "auto"}))
            self.assertIn("KID1", restore.pending())
            self.assertIsNone(hook.respond({"hook_event_name": "PostToolUse", "session_id": "PARENT1", "agent_id": "OTHER", "tool_name": "exec"}),
                              "another thread's tool call does not consume the marker")
            out = hook.respond({"hook_event_name": "PostToolUse", "session_id": "PARENT1", "agent_id": "KID1", "tool_name": "exec"})
            ctx = out["hookSpecificOutput"]["additionalContext"]
            self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "PostToolUse")
            self.assertIn("compacted", ctx)
            self.assertIn("KID1", ctx)
            self.assertIsNone(hook.respond({"hook_event_name": "PostToolUse", "session_id": "PARENT1", "agent_id": "KID1"}), "injected once")
            hook.respond({"hook_event_name": "PostCompact", "session_id": "MAIN1"})
            hook.respond({"hook_event_name": "SessionStart", "session_id": "MAIN1", "source": "compact"})
            self.assertNotIn("MAIN1", restore.pending(), "a main session is restored by SessionStart, so no second injection")
            hook.respond({"hook_event_name": "PostCompact", "session_id": "PARENT1", "agent_id": "KID2"})
            hook.respond({"hook_event_name": "SubagentStop", "session_id": "PARENT1", "agent_id": "KID2"})
            self.assertIn("KID2", restore.pending(), "a subagent that answers without a tool call is reloaded in its next turn")
            self.assertIsNotNone(hook.respond({"hook_event_name": "PostToolUse", "session_id": "PARENT1", "agent_id": "KID2"}))
        finally:
            config.BASE_URL = saved
        c = fresh_db()
        with db.write(c):
            c.execute("insert into sessions(id, status, archived, root_id) values('KID3','archived',1,'KID3'), ('KID4','done',0,'ROOT4'), "
                      "('ROOT4','waiting',0,'ROOT4'), ('KID6','done',0,'ROOT6'), ('ROOT6','archived',1,'ROOT6')")
        restore.mark("KID3")
        restore.mark("KID4")
        restore.mark("KID6")
        dead = restore._dir() / "KID5.999999"
        dead.write_text("x")
        restore.prune(c)
        self.assertEqual([t for t in restore.pending() if t in ("KID3", "KID4", "KID6")], ["KID4"],
                         "markers of archived threads and of threads under an archived root are pruned")
        self.assertFalse(dead.exists(), "a claim file left by a dead hook process is removed")
        restore.clear("KID4")

    def test_marker_problems_never_cost_the_digest(self):
        from tracker import restore
        saved_dir, saved_url = config.DATA_DIR, config.BASE_URL
        blocker = TMP / f"not-a-dir-{time.time_ns()}"
        blocker.write_text("x")
        config.DATA_DIR, config.BASE_URL = blocker, "http://127.0.0.1:9"
        try:
            restore.mark("M1")
            out = hook.respond({"hook_event_name": "SessionStart", "session_id": "M1", "source": "compact"})
            self.assertIn("M1", out["hookSpecificOutput"]["additionalContext"])
            self.assertIsNone(hook.respond({"hook_event_name": "PostToolUse", "session_id": "M1"}))
        finally:
            config.DATA_DIR, config.BASE_URL = saved_dir, saved_url

    def test_posttool_shell_gate(self):
        import subprocess
        gate = str(config.PROJECT_DIR / "bin" / "st-hook-posttool")
        data = TMP / f"gate-{time.time_ns()}"
        data.mkdir()
        sentinel = data / "python-started"
        stub = data / "stub-python"
        stub.write_text(f"#!/bin/sh\ntouch '{sentinel}'\ncat >/dev/null\n")
        stub.chmod(0o755)
        env = dict(os.environ, ST_DATA_DIR=str(data), ST_BASE_URL="http://127.0.0.1:9")
        payload = json.dumps({"hook_event_name": "PostToolUse", "session_id": "S9", "agent_id": "K9", "tool_name": "exec",
                              "tool_response": "x" * 200000 + ' "session_id": "NOTME" '})
        def run(py):
            sentinel.unlink(missing_ok=True)
            r = subprocess.run(["/bin/sh", gate, str(py)], input=payload, capture_output=True, text=True, env=env, timeout=20)
            return r, sentinel.exists()
        r, started = run(stub)
        self.assertEqual((r.returncode, r.stdout, started), (0, "", False), "no restore folder: Python is not started")
        (data / "restore").mkdir()
        (data / "restore" / "OTHER").write_text("OTHER")
        r, started = run(stub)
        self.assertEqual((r.returncode, r.stdout, started), (0, "", False), "another thread's pending reload does not start Python")
        (data / "restore" / "K9").write_text("K9")
        r, started = run(stub)
        self.assertTrue(started, "this thread's pending reload starts Python")
        loud, _ = run(sys.executable)
        self.assertEqual(loud.returncode, 0)
        self.assertIn("compacted", json.loads(loud.stdout)["hookSpecificOutput"]["additionalContext"])
        self.assertFalse((data / "restore" / "K9").exists())
        self.assertTrue((data / "restore" / "OTHER").exists(), "other threads' markers are left alone")

if __name__ == "__main__":
    unittest.main()

