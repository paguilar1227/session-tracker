"""Real-session checks: does a Codex session actually use the tracker after compaction, inside a compacted subagent, and
while orchestrating subagents whose results interleave with its own work?

Runs real 'codex exec' sessions (this costs model calls). Facts are planted in the tracker while a session is already
running, so they can only reach the model through the tracker: a digest the hooks inject, or the session_tracker tools.
Each scenario is one turn, because resuming a session re-injects the digest at SessionStart and would hide what
compaction does.

    python3 tests/e2e/sessions.py SCENARIO [OUT_DIR]
    SCENARIO: main | main_control | subagent | subagent_baseline | orchestrate | reeval (recompute evidence in OUT_DIR)
              | cleanup (delete the runs' tracker todos and ledger entries; backup in OUT_DIR)

'--dangerously-bypass-hook-trust' is passed (except for subagent_baseline) so a session-tracker hook that is installed
but not yet approved in Codex still runs in these throwaway sessions; it does not change your approvals.
"""
import json
import os
import pathlib
import random
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tracker import config  # noqa: E402
from tracker.install import HOOK_TIMEOUT_SECONDS  # noqa: E402

MODEL = os.environ.get("ST_E2E_MODEL", "")  # empty: Codex's configured default model
EFFORT = os.environ.get("ST_E2E_EFFORT", "medium")
# Measured: a fresh session on this machine starts at about 45k input tokens (AGENTS.md, skills, tools, digest), and the
# four files below add about 30k. 60k forces a compaction partway through while a compacted context still fits.
COMPACT_AT = int(os.environ.get("ST_E2E_COMPACT_AT", "60000"))
WORK = pathlib.Path(os.environ.get("ST_E2E_WORK", "/tmp/st-e2e-work"))


def filler():
    WORK.mkdir(parents=True, exist_ok=True)
    rnd = random.Random(7)
    words = ("alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima mike november oscar papa quebec "
             "romeo sierra tango uniform victor whiskey xray yankee zulu").split()
    for n in range(1, 5):
        path = WORK / f"big{n}.txt"
        if not path.exists():
            rows = [f"{n}-{i:04d} " + " ".join(rnd.choice(words) for _ in range(9)) for i in range(450)]
            path.write_text("\n".join(rows) + f"\nEND OF FILE {n}\n")
    return ", ".join(f"\x60cat {WORK / f'big{n}.txt'}\x60" for n in range(1, 5))


def api(method, path, body=None):
    req = urllib.request.Request(config.BASE_URL + path, method=method, data=None if body is None else json.dumps(body).encode(),
                                 headers={"X-Actor": "user", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=HOOK_TIMEOUT_SECONDS) as r:
        return json.load(r)


def ro(path):
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    c.row_factory = sqlite3.Row
    return c


class Run:
    def __init__(self, out, tag, prompt, compact=True, bypass=True):
        self.log = out / f"{tag}.jsonl"
        args = ["codex", "exec", "--json", "--skip-git-repo-check"] + (["-m", MODEL] if MODEL else []) + \
            ["-c", f"model_reasoning_effort={EFFORT}", "-C", str(WORK)]
        if compact:
            args += ["-c", f"model_auto_compact_token_limit={COMPACT_AT}"]
        if bypass:
            args.append("--dangerously-bypass-hook-trust")
        self.started = int(time.time() * 1000)
        self.proc = subprocess.Popen(args + [prompt], stdout=open(self.log, "w"), stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, cwd=WORK)

    def alive(self):
        return self.proc.poll() is None

    def events(self):
        out = []
        for line in open(self.log):
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
        return out

    def thread(self):
        while True:
            for e in self.events():
                if e.get("type") == "thread.started":
                    return e["thread_id"]
            if not self.alive():
                return None
            time.sleep(0.5)

    def final(self):
        msgs = [e["item"].get("text", "") for e in self.events() if e.get("type") == "item.completed" and e["item"].get("type") == "agent_message"]
        return msgs[-1] if msgs else ""


def wait_tracked(sid, run):
    while run.alive():
        try:
            api("GET", f"/api/sessions/{sid}?rescan=0")
            return True
        except urllib.error.HTTPError:
            time.sleep(1)
    return False


def children(parent):
    with ro(config.codex_state_db()) as c:
        return [r[0] for r in c.execute("select child_thread_id from thread_spawn_edges where parent_thread_id=? order by rowid", (parent,))]


def wait_children(parent, n, run):
    while run.alive():
        kids = children(parent)
        if len(kids) >= n:
            return kids
        time.sleep(1)
    return children(parent)


def plant(sid, todos=(), ledger=()):
    for t in todos:
        api("POST", f"/api/sessions/{sid}/todos", t)
    for e in ledger:
        api("POST", f"/api/sessions/{sid}/ledger", e)


def evidence(thread, since, tokens, is_child=False):
    """How the planted facts reached this thread, and what the hooks did, as an ordered timeline from its transcript."""
    with ro(config.DB_PATH) as c:
        rows = [dict(r) for r in c.execute("select type, session_id, payload from events where source='hook' and ts>=? order by ts", (since,))]
    def mine(r):
        p = json.loads(r["payload"] or "{}")
        return (p.get("agent_id") == thread) if is_child else (r["session_id"] == thread and not p.get("agent_id"))
    ev = [(r["type"], json.loads(r["payload"] or "{}")) for r in rows if mine(r)]
    with ro(config.codex_state_db()) as c:
        row = c.execute("select rollout_path from threads where id=?", (thread,)).fetchone()
    timeline, calls = [], []
    if row and row[0] and os.path.exists(row[0]):
        for line in open(row[0]):
            try:
                d = json.loads(line)
            except ValueError:
                continue
            p = d.get("payload") or {}
            kind = p.get("type")
            if d.get("type") == "compacted" or kind == "context_compacted":
                timeline.append("compacted")
                continue
            item = p.get("item") or {}
            if kind == "item_completed" and item.get("type") == "McpToolCall" and item.get("server") == "session_tracker":
                calls.append(item.get("tool"))
                timeline.append(f"tracker:{item.get('tool')}")
                continue
            if kind == "message" and p.get("role") in ("developer", "assistant"):
                text = json.dumps(p).lower()
                hits = [t for t in tokens if t in text]
                if hits:
                    timeline.append(("injected" if p["role"] == "developer" else "answer") + ":" + ",".join(hits))
    return {
        "compactions": sum(1 for t, _ in ev if t == "PostCompact"),
        "session_start_compact": sum(1 for t, p in ev if t == "SessionStart" and p.get("source") == "compact"),
        "posttool_restores": sum(1 for t, p in ev if t == "PostToolUse" and p.get("st_restored")),
        "tracker_tool_calls": calls,
        "timeline": [x for i, x in enumerate(timeline) if i == 0 or x != timeline[i - 1]],
    }


def reeval(out):
    """Recompute the evidence for finished runs in OUT (after changing what is measured)."""
    for path in sorted(out.glob("*.result.json")):
        d = json.loads(path.read_text())
        tokens = {"main": MAIN_TOKENS, "main_control": MAIN_TOKENS, "subagent": KID_TOKENS, "subagent_baseline": KID_TOKENS}.get(d["scenario"], ["release notes"])
        if d["scenario"].startswith("subagent"):
            d.update(evidence(d["child"], 0, tokens, is_child=True))
            d["facts_in_answer"] = score(d.get("child_answer"), tokens)
        else:
            thread = d.get("thread") or d.get("parent")
            d.update(evidence(thread, 0, tokens))
            if d["scenario"].startswith("main"):
                d["facts_in_answer"] = score(d.get("answer"), tokens)
        d.pop("fact_reached_via", None)
        path.write_text(json.dumps(d, indent=2))


def child_final(child):
    with ro(config.codex_history_db()) as c:
        rows = c.execute("select item_json from thread_items where thread_id=? and item_type='agentMessage' order by rollout_ordinal", (child,)).fetchall()
    return json.loads(rows[-1][0]).get("text", "") if rows else ""


def score(text, tokens):
    low = (text or "").lower()
    return {t: t in low for t in tokens}


MAIN_TODO = {"title": "Rename the S3 bucket to atlas-prod-2", "priority": "P1", "state": "blocked",
             "notes": "Waiting on Dana from infra to approve the bucket policy."}
MAIN_LEDGER = [
    {"kind": "decision", "title": "Use Postgres 17 for Atlas, not MySQL", "status": "decided", "rationale": "We need logical replication slots."},
    {"kind": "mistake", "title": "Ran terraform apply without -target in the Atlas repo", "body": "A full apply was run from the Atlas repo.",
     "rationale": "It modified the shared VPC.", "lesson": "In the Atlas repo, never run terraform apply without -target; a full apply touches the shared VPC."},
]
MAIN_TOKENS = ["atlas-prod-2", "postgres", "terraform"]
KID_TODO = {"title": "Re-run the tenant 42 import after the schema fix", "priority": "P1", "state": "blocked", "notes": "Blocked on migration 0042."}
KID_LEDGER = [{"kind": "mistake", "title": "Truncated the staging tables during the import", "body": "The staging tables were truncated.",
               "rationale": "It deleted rows other tenants needed.", "lesson": "Never truncate the staging tables; set the soft-delete flag instead."}]
KID_TOKENS = ["tenant 42", "soft-delete"]


def main_scenario(out, tag, compact):
    cats = filler()
    prompt = (f"We're picking the Atlas migration back up. Do these steps in order:\n1. Run \x60sleep 30\x60.\n"
              f"2. Run each of these commands, one at a time: {cats}. Don't summarize them.\n"
              "3. Then give me a quick check-in as three short bullets: what is still open for us, what we decided about the "
              "database, and anything I told you to avoid.")
    run = Run(out, tag, prompt, compact=compact)
    sid = run.thread()
    planted = wait_tracked(sid, run)
    if planted:
        plant(sid, [MAIN_TODO], MAIN_LEDGER)
    run.proc.wait()
    final = run.final()
    return {"scenario": tag, "thread": sid, "planted_while_running": planted, "exit": run.proc.returncode,
            "answer": final, "facts_in_answer": score(final, MAIN_TOKENS), **evidence(sid, run.started, MAIN_TOKENS)}


def subagent_scenario(out, tag, bypass):
    cats = filler()
    task = ("You are continuing the tenant data import. Do these steps in order:\n1. Run \x60sleep 30\x60.\n"
            f"2. Run each of these commands, one at a time: {cats}. Don't summarize them.\n"
            "3. Your final answer: one sentence on what is still open for you, and one sentence on anything you have been told to avoid.")
    prompt = ("Spawn exactly one subagent and give it this task, word for word:\n---\n" + task +
              "\n---\nWait for the subagent to finish, then reply with its final answer verbatim and nothing else.")
    run = Run(out, tag, prompt, bypass=bypass)
    parent = run.thread()
    kids = wait_children(parent, 1, run)
    kid = kids[0] if kids else None
    planted = bool(kid) and wait_tracked(kid, run)
    if planted:
        plant(kid, [KID_TODO], KID_LEDGER)
    run.proc.wait()
    final = child_final(kid) if kid else ""
    return {"scenario": tag, "parent": parent, "child": kid, "planted_while_running": planted, "exit": run.proc.returncode,
            "child_answer": final, "parent_answer": run.final(), "facts_in_answer": score(final, KID_TOKENS),
            **(evidence(kid, run.started, KID_TOKENS, is_child=True) if kid else {})}


def orchestrate_scenario(out, tag):
    cats = filler()
    prompt = ("You're coordinating three subtasks. Spawn three subagents in parallel with these tasks, word for word:\n"
              "A: \"Run \x60sleep 45\x60. Then write a one-paragraph explanation of vector clocks to a Markdown file named "
              "vector-clocks.md in your session's artifacts folder. Reply DONE-A.\"\n"
              "B: \"Run \x60sleep 10\x60. Then choose between SQLite and DuckDB for an embedded analytics cache, record the choice "
              "and the reason as a decision in the session tracker, and reply DONE-B with the choice.\"\n"
              "C: \"Run \x60sleep 25\x60. Then list three risks of running cron jobs on laptops. Reply DONE-C followed by the list.\"\n"
              f"While they run, do your own work: run each of these commands, one at a time: {cats}. Don't summarize them.\n"
              "Handle each subagent's result as it arrives. When all three are done and your own work is finished, give me a "
              "status report: for each subtask, its state and what it produced (files, decisions), then what is still open for us.")
    run = Run(out, tag, prompt)
    parent = run.thread()
    kids = wait_children(parent, 3, run)
    planted = wait_tracked(parent, run)
    if planted:
        plant(parent, [{"title": "Send the Atlas release notes to Dana by Friday", "priority": "P1", "state": "not_started"}])
    run.proc.wait()
    final = run.final()
    view = api("GET", f"/api/sessions/{parent}?rescan=1")
    truth = []
    for ch in view["children"]:
        cv = api("GET", f"/api/sessions/{ch['id']}?rescan=1")
        truth.append({"id": ch["id"], "status": ch["status"], "artifacts": [a.get("path", "").split("/")[-1] for a in cv["artifacts"] if a.get("path")],
                      "decisions": [d["title"] for d in cv["ledger"]["decision"]]})
    tokens = ["vector-clocks.md", "cron", "release notes"]
    return {"scenario": tag, "parent": parent, "children": kids, "planted_while_running": planted, "exit": run.proc.returncode,
            "answer": final, "facts_in_answer": score(final, tokens + ["sqlite", "duckdb"]), "tracker_truth": truth,
            **evidence(parent, run.started, ["release notes"])}


def cleanup(out):
    """Remove what these runs left in the tracker: planted and scribe-written todos and ledger entries in every session
    whose folder is under /tmp/st-e2e*, so fake lessons never surface in real sessions' search. Rows are backed up to
    OUT/cleanup-backup.json first. Archive the threads in Codex if you also want them out of the session list."""
    with ro(config.DB_PATH) as c:
        roots = [r[0] for r in c.execute("select id from sessions where cwd like '/tmp/st-e2e%' or cwd like '/private/tmp/st-e2e%'")]
        ids = set(roots) | {r[0] for r in c.execute(
            f"select id from sessions where root_id in ({','.join('?' * len(roots))})", roots)} if roots else set()
        marks = ",".join("?" * len(ids))
        todos = [dict(r) for r in c.execute(f"select * from todos where session_id in ({marks})", list(ids))] if ids else []
        ledger = [dict(r) for r in c.execute(f"select * from ledger where session_id in ({marks})", list(ids))] if ids else []
    out.mkdir(parents=True, exist_ok=True)
    (out / "cleanup-backup.json").write_text(json.dumps({"sessions": sorted(ids), "todos": todos, "ledger": ledger}, indent=2))
    for t in todos:
        api("DELETE", f"/api/todos/{t['id']}")
    for e in ledger:
        api("DELETE", f"/api/ledger/{e['id']}")
    print(json.dumps({"sessions": len(ids), "todos_deleted": len(todos), "ledger_deleted": len(ledger),
                      "backup": str(out / "cleanup-backup.json"), "thread_ids": sorted(ids)}))


def main(argv):
    scenario = argv[1]
    out = pathlib.Path(argv[2] if len(argv) > 2 else f"/tmp/st-e2e-{time.strftime('%Y%m%d-%H%M%S')}")
    if scenario == "reeval":
        return reeval(out)
    if scenario == "cleanup":
        return cleanup(out)
    out.mkdir(parents=True, exist_ok=True)
    result = {"main": lambda: main_scenario(out, "main", True),
              "main_control": lambda: main_scenario(out, "main_control", False),
              "subagent": lambda: subagent_scenario(out, "subagent", True),
              "subagent_baseline": lambda: subagent_scenario(out, "subagent_baseline", False),
              "orchestrate": lambda: orchestrate_scenario(out, "orchestrate")}[scenario]()
    result.update(model=MODEL, effort=EFFORT, compact_at=COMPACT_AT)
    (out / f"{scenario}.result.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main(sys.argv)
