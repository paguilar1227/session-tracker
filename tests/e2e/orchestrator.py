"""Orchestrator A/B test: does the session tracker keep one long-lived orchestrator thread on track while it runs many
subagents, is interrupted, and is compacted again and again?

One run is one real Codex thread driven through 'codex app-server' by a fixed script, identical for both arms:
ten background jobs handed out in batches, one job cancelled up front and one mid-turn, one job redefined, unrelated
work in between (some of it while results land), a status request steered into a running turn, and five compactions.
Every job blocks on a gate that this harness opens, so which result lands before which compaction or checkpoint is the
same in every run. At three checkpoints the orchestrator must report every job's state and result as JSON; that is
scored against what actually happened (results as the subagents reported them, user instructions as given).

Arms:
  tracker  everything as installed: hooks, session_tracker MCP tools, skill
  control  tracker hooks silenced (ST_DISABLE=1) and the session_tracker MCP server disabled, for parent and children

    python3 tests/e2e/orchestrator.py run ARM OUT_DIR [--effort EFFORT] [--model MODEL]
    python3 tests/e2e/orchestrator.py score OUT_DIR [OUT_DIR ...]
"""
import argparse
import json
import os
import pathlib
import re
import secrets
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from appserver import AppServer  # noqa: E402
from sessions import filler, WORK  # noqa: E402

MODEL = os.environ.get("ST_E2E_MODEL") or None  # None: Codex's configured default model
TASKS = [f"T{i}" for i in range(1, 11)]
GATE = '''import os, sys, time
path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gates", sys.argv[1])
while not os.path.exists(path):
    time.sleep(1)
print(open(path).read().strip(), flush=True)
'''
FENCE = "\x60\x60\x60"
CHECKPOINT = ("Status check on the background jobs. Reply with only a JSON code block, no other text, listing every job I "
              "have given you in this conversation, including cancelled ones:\n" + FENCE + "json\n"
              '{"jobs": [{"id": "T1", "state": "done | running | cancelled | not_started", "result": "6-character code or null"}]}\n'
              + FENCE + "\nstate is done only if the job's subagent has reported its result; result is the code it reported. "
              "Check whatever you need to before answering.")
STATES = {"done": "done", "completed": "done", "complete": "done", "finished": "done", "succeeded": "done",
          "running": "running", "in_progress": "running", "in progress": "running", "pending": "running",
          "waiting": "running", "active": "running", "started": "running",
          "cancelled": "cancelled", "canceled": "cancelled", "closed": "cancelled", "aborted": "cancelled", "stopped": "cancelled",
          "not_started": "not_started", "not started": "not_started", "queued": "not_started", "todo": "not_started"}


SETTLED = ("idle", "systemError")


class Run:
    STOP_ON_FAILED_TURN = False
    TASK_IDS = TASKS
    REDEFINED = ["T5b"]
    EXTRA_CONFIG = []

    def __init__(self, arm, out, effort, model):
        self.arm, self.out, self.effort, self.model = arm, out, effort, model
        out.mkdir(parents=True, exist_ok=True)
        filler()
        (WORK / "gates").mkdir(exist_ok=True)
        (WORK / "gate.py").write_text(GATE)
        self.salt = secrets.token_hex(3)
        self.tokens = {t: f"{t}-{self.salt}" for t in self.TASK_IDS + self.REDEFINED}
        self.codes = {k: secrets.token_hex(3) for k in self.tokens}
        # Enough agent slots for every subagent the script asks for (11) plus the orchestrator, so Codex never refuses a
        # spawn. A refused spawn retried in a later turn can fail the whole turn in both arms, which is not what is measured.
        cfg = [f"model_reasoning_effort={effort}", "memories.generate_memories=false",
               f"features.multi_agent_v2.max_concurrent_threads_per_session={getattr(self, 'SLOTS', len(self.tokens) + 1)}"] + self.EXTRA_CONFIG
        env = {}
        if arm == "control":
            cfg.append("mcp_servers.session_tracker.enabled=false")
            env["ST_DISABLE"] = "1"
        self.s = AppServer(out / "rpc.jsonl", config=cfg, env=env, cwd=str(WORK), stderr_path=out / "appserver.stderr")
        self.s.initialize()
        t = self.s.request("thread/start", {**({"model": model} if model else {}), "cwd": str(WORK), "approvalPolicy": "never",
                                            "sandbox": "danger-full-access"})
        self.tid = t["thread"]["id"]
        self.started = time.time()
        self.events, self.turns, self.checkpoints = [], [], []
        self.children, self.results, self.status = {}, {}, {}
        self.parent_turns, self.tracker_calls, self.hooks, self.parent_gate = [], [], [], []
        self.child_msgs, self.compaction_times = [], []
        self.cursor = 0
        self.log(f"{arm}: thread {self.tid} salt {self.salt}")

    def log(self, msg):
        line = f"[{time.strftime('%H:%M:%S')} {self.arm}] {msg}"
        print(line, flush=True)
        with open(self.out / "progress.log", "a") as f:
            f.write(line + "\n")

    def job(self, task, key=None):
        token = self.tokens[key or task]
        return (f"{task}: run python3 {WORK / 'gate.py'} {token} and wait for it. It blocks until the job is finished, which "
                "can take many minutes, so keep waiting (poll with long waits) until it prints a 6-character code. Then reply "
                f"with exactly one line: {task} RESULT <code>")

    def update(self):
        notes = self.s.notes
        while self.cursor < len(notes):
            n = notes[self.cursor]
            self.cursor += 1
            m, p = n.get("method"), n.get("params") or {}
            th, t = p.get("threadId"), n.get("_t")
            if m == "thread/status/changed":
                self.status[th] = (p.get("status") or {}).get("type")
            elif m == "turn/started" and th == self.tid:
                self.parent_turns.append({"t": t, "turn": p["turn"]["id"]})
            elif m in ("item/started", "item/completed"):
                it = p.get("item") or {}
                ty = it.get("type")
                if ty == "subAgentActivity" and th == self.tid and it.get("kind") == "started":
                    self.children.setdefault(it["agentThreadId"], {"path": it.get("agentPath"), "tokens": [], "t": t})
                elif ty == "commandExecution" and m == "item/started":
                    cmd = it.get("command") or ""
                    for k, tok in self.tokens.items():
                        if tok in cmd:
                            if th == self.tid:
                                self.parent_gate.append({"t": t, "key": k})
                            else:
                                c = self.children.setdefault(th, {"path": None, "tokens": [], "t": t})
                                if k not in c["tokens"]:
                                    c["tokens"].append(k)
                elif ty == "contextCompaction" and m == "item/completed" and th == self.tid:
                    self.compaction_times.append(t)
                elif ty == "agentMessage" and m == "item/completed" and th != self.tid:
                    text = it.get("text") or ""
                    self.child_msgs.append({"t": t, "thread": th, "text": text[:4000]})
                    for k, code in self.codes.items():
                        if code in text and k not in self.results:
                            self.results[k] = {"t": t, "thread": th, "text": text[:200]}
                elif ty == "mcpToolCall" and m == "item/completed" and th == self.tid:
                    self.tracker_calls.append({"t": t, "server": it.get("server"), "tool": it.get("tool")})
            elif m == "hook/completed":
                run = p.get("run") or {}
                if run.get("entries"):
                    self.hooks.append({"t": t, "thread": th, "event": run.get("eventName"), "status": run.get("status"),
                                       "entries": run["entries"]})

    def compactions(self):
        self.update()
        return sum(1 for n in self.s.notes[:self.cursor] if n.get("method") == "item/completed"
                   and (n.get("params") or {}).get("threadId") == self.tid
                   and ((n.get("params") or {}).get("item") or {}).get("type") == "contextCompaction")

    def gate_alive(self, key):
        return subprocess.run(["pgrep", "-f", f"gate.py {self.tokens[key]}"], capture_output=True).returncode == 0

    def wait_parent_idle(self):
        while True:
            self.update()
            if self.status.get(self.tid) != "active":
                return
            time.sleep(2)

    def settle(self, label):
        """Wait until the parent is idle and every child is idle or blocked on its gate."""
        last = 0
        while True:
            self.update()
            busy = self.status.get(self.tid) == "active"
            pending = [c for c, i in self.children.items()
                       if self.status.get(c) not in SETTLED and not any(self.gate_alive(k) for k in i["tokens"])]
            if not busy and not pending:
                return
            if time.time() - last > 60:
                self.log(f"settle {label}: parent {'busy' if busy else 'idle'}, {len(pending)} child(ren) starting or finishing")
                last = time.time()
            time.sleep(2)

    def release(self, *keys):
        for k in keys:
            (WORK / "gates" / self.tokens[k]).write_text(self.codes[k])
            self.events.append({"t": time.time(), "kind": "release", "key": k})
        self.log(f"released {', '.join(keys)}")

    def wait_results(self, *keys):
        last = 0
        while True:
            self.update()
            left = [k for k in keys if k not in self.results
                    and any(self.status.get(c) not in SETTLED for c, i in self.children.items() if k in i["tokens"])]
            if not left:
                missing = [k for k in keys if k not in self.results]
                if missing:
                    self.log(f"no result for {', '.join(missing)} (no live child holds it)")
                return
            if time.time() - last > 60:
                self.log(f"waiting for results: {', '.join(left)}")
                last = time.time()
            time.sleep(2)

    def instruct(self, kind, task, t, key=None):
        self.events.append({"t": t, "kind": kind, "task": task, "key": key or task})

    def turn(self, label, text, during=None, steer=None):
        self.wait_parent_idle()
        m = self.s.mark()
        t0 = time.time()
        turn_id = self.s.request("turn/start", {"threadId": self.tid, "input": [{"type": "text", "text": text}]})["turn"]["id"]
        self.log(f"turn {label} started")
        steer_t = None
        if steer:
            _, n = self.s.wait_for(lambda n: (n.get("method") == "item/completed" and n["params"].get("threadId") == self.tid
                                              and n["params"]["item"].get("type") == "commandExecution")
                                   or (n.get("method") == "turn/completed" and n["params"].get("threadId") == self.tid), m)
            if n["method"] == "item/completed":
                try:
                    self.s.request("turn/steer", {"threadId": self.tid, "expectedTurnId": turn_id,
                                                  "input": [{"type": "text", "text": steer}]})
                    steer_t = time.time()
                    self.log(f"steered into {label}")
                except RuntimeError as e:
                    self.log(f"steer failed: {e}")
        if during:
            during()
        _, done = self.s.wait_for(lambda n: n.get("method") == "turn/completed" and n["params"]["threadId"] == self.tid
                                  and n["params"]["turn"]["id"] == turn_id, m)
        replies = [n["params"]["item"].get("text") for n in self.s.notes[m:]
                   if n.get("method") == "item/completed" and n["params"].get("threadId") == self.tid
                   and n["params"]["item"].get("type") == "agentMessage"]
        rec = {"label": label, "turn": turn_id, "t0": t0, "t1": time.time(), "status": done["params"]["turn"].get("status"),
               "reply": replies[-1] if replies else None, "steered_at": steer_t}
        self.turns.append(rec)
        self.update()
        self.log(f"turn {label} {rec['status']} in {rec['t1'] - t0:.0f}s")
        if rec["status"] == "failed" and self.STOP_ON_FAILED_TURN:
            raise RuntimeError(f"turn {label} failed; stopping (see rpc.jsonl for the error)")
        if steer and steer_t is None:
            rec2 = self.turn(label + "-steer", steer)
            steer_t = rec2["t0"]
        rec["instruction_t"] = steer_t
        return rec

    def compact(self, label):
        self.wait_parent_idle()
        m = self.s.mark()
        t0 = time.time()
        self.s.request("thread/compact/start", {"threadId": self.tid})
        self.s.wait_for(lambda n: n.get("method") == "turn/completed" and n["params"].get("threadId") == self.tid, m)
        ok = any(n.get("method") == "item/completed" and n["params"].get("threadId") == self.tid
                 and n["params"]["item"].get("type") == "contextCompaction" for n in self.s.notes[m:])
        self.events.append({"t": t0, "kind": "compact", "label": label, "ok": ok, "t1": time.time()})
        self.log(f"compaction {label} {'ok' if ok else 'NOT SEEN'}")

    def checkpoint(self, label):
        rec = self.turn(label, CHECKPOINT)
        self.checkpoints.append({"label": label, "t0": rec["t0"], "t1": rec["t1"], "reply": rec["reply"]})

    @classmethod
    def rebuild(cls, out):
        """run.json from the logs of a harness that died before writing it: replay its notifications through update()
        and recover turns, checkpoints and instructions from rpc.jsonl and progress.log. Board snapshots are not recoverable."""
        prog = (out / "progress.log").read_text()
        tid, salt = re.search(r"thread (\S+) salt (\S+)", prog).groups()
        labels = re.findall(r"\] turn (\S+) started", prog)
        rows = [json.loads(x) for x in (out / "rpc.jsonl").read_text().splitlines() if x.strip()]
        notes, starts, resp = [], [], {}
        for r in rows:
            if "in" in r and "method" in r["in"] and "id" not in r["in"]:
                notes.append(dict(r["in"], _t=r["t"]))
            elif "in" in r and "id" in r["in"]:
                resp[r["in"]["id"]] = r["in"].get("result") or {}
            elif "out" in r and r["out"].get("method") == "turn/start" and r["out"]["params"].get("threadId") == tid:
                starts.append((r["out"]["id"], r["t"]))
            elif "out" in r and r["out"].get("method") == "thread/start":
                model, t_start = r["out"]["params"].get("model"), r["t"]
        self = cls.__new__(cls)
        self.arm, self.out, self.effort, self.model, self.tid, self.salt = out.name.split("-")[-1], out, "medium", model, tid, salt
        self.started, self.tokens, self.codes = t_start, {}, {}
        self.events, self.turns, self.checkpoints, self.children, self.results, self.status = [], [], [], {}, {}, {}
        self.parent_turns, self.tracker_calls, self.hooks, self.parent_gate, self.child_msgs, self.compaction_times = [], [], [], [], [], []
        self.cursor, self.board_snaps, self.answers = 0, [], {}
        self.dir = WORK / salt
        self.tasks = {f"T{3 * (k - 1) + j + 1:02d}": secs for k, sl in BACKLOG_SLEEPS.items() for j, secs in enumerate(sl)}
        self.s = type("Notes", (), {"notes": notes})()
        self.update()
        for label, (rid, t0) in zip(labels, starts):
            turn_id = ((resp.get(rid) or {}).get("turn") or {}).get("id")
            done = next((n for n in notes if n.get("method") == "turn/completed" and n["params"].get("threadId") == tid
                         and n["params"]["turn"]["id"] == turn_id), None)
            t1 = done["_t"] if done else None
            replies = [n["params"]["item"].get("text") for n in notes if n.get("method") == "item/completed"
                       and n["params"].get("threadId") == tid and n["params"]["item"].get("type") == "agentMessage"
                       and t0 <= n["_t"] <= (t1 or float("inf"))]
            rec = {"label": label, "turn": turn_id, "t0": t0, "t1": t1, "status": done["params"]["turn"].get("status") if done else None,
                   "reply": replies[-1] if replies else None, "steered_at": None, "instruction_t": None}
            self.turns.append(rec)
            if label.startswith("CP-"):
                self.checkpoints.append({"label": label, "t0": t0, "t1": t1, "reply": rec["reply"]})
            if label.startswith("Q-"):
                self.answers[label] = {"t0": t0, "t1": t1, "reply": rec["reply"]}
            kind, tasks = {"U1-setup": ("introduce", [f"T{i:02d}" for i in range(1, 31)]), "U4-drop": ("cancel", ["T21", "T22"]),
                           "U5-add": ("introduce", ["T31", "T32", "T33"]), "U6-retry": ("retry", ["T13"])}.get(label, (None, []))
            for t in tasks:
                self.instruct(kind, t, t0)
            if label == "U5-add":
                self.tasks.update({t: 45 for t in tasks})
        self.events.append({"t": time.time(), "kind": "rebuilt", "why": "harness hung at drain on systemError children and was killed"})
        data = {"arm": self.arm, "effort": self.effort, "model": self.model, "thread": self.tid, "salt": self.salt,
                "started": self.started, "ended": time.time(), "error": "rebuilt from logs", "tokens": {}, "codes": {},
                "events": self.events, "turns": self.turns, "checkpoints": self.checkpoints, "children": self.children,
                "results": self.results, "parent_turns": self.parent_turns, "tracker_calls": self.tracker_calls,
                "hooks": self.hooks, "parent_gate": self.parent_gate, "child_msgs": self.child_msgs,
                "compaction_times": self.compaction_times, **self.extra_state()}
        (out / "run.json").write_text(json.dumps(data, indent=1))
        return data

    @classmethod
    def resume(cls, out):
        """Continue a run whose harness stopped after its drain: same thread, fresh app-server, remaining tail steps.
        Hooks stay off (ST_DISABLE) so the tracker arm gets no extra resume digest an uninterrupted run would not have had."""
        if not (out / "run.json").exists():
            cls.rebuild(out)
        data = json.loads((out / "run.json").read_text())
        self = cls.__new__(cls)
        self.out = out
        for k, v in data.items():
            setattr(self, {"thread": "tid", "dir": "dir_str"}.get(k, k), v)
        self.dir = pathlib.Path(data["dir"]) if data.get("dir") else None
        self.error = None
        cfg = [f"model_reasoning_effort={self.effort}", "memories.generate_memories=false",
               f"features.multi_agent_v2.max_concurrent_threads_per_session={getattr(self, 'SLOTS', len(self.tokens) + 1)}"] + self.EXTRA_CONFIG
        env = {"ST_DISABLE": "1"}
        if self.arm == "control":
            cfg.append("mcp_servers.session_tracker.enabled=false")
        self.s = AppServer(out / "rpc.jsonl", config=cfg, env=env, cwd=str(WORK), stderr_path=out / "appserver.stderr")
        self.s.initialize()
        self.s.request("thread/resume", {"threadId": self.tid, **({"model": self.model} if self.model else {}), "cwd": str(WORK), "approvalPolicy": "never",
                                         "sandbox": "danger-full-access"})
        self.status, self.cursor = {}, 0
        self.events.append({"t": time.time(), "kind": "resumed", "after_error": data.get("error")})
        self.log(f"resumed thread {self.tid} after: {data.get('error')}")
        return self

    def tail(self):
        return []

    def script(self):
        intro = ("I'm going to hand you background jobs while we keep talking about other things. Delegate each job to its "
                 "own subagent, passing the job text verbatim, and don't wait for them: end your turn once they are "
                 "dispatched. Keep track of them; I'll ask for status now and then.\n\n")
        rec = self.turn("U1-dispatch", intro + "\n".join(f"- {self.job(t)}" for t in TASKS[:6]))
        for t in TASKS[:6]:
            self.instruct("introduce", t, rec["t0"])
        self.settle("U1")
        self.log(f"U1 spawned {len(self.children)} subagent(s)")

        self.release("T1", "T2")
        self.wait_results("T1", "T2")
        self.turn("U2-distraction", f"Unrelated, while those run: cat {WORK}/big1.txt and {WORK}/big2.txt in full (I want "
                                    "you to actually read them), then tell me which word appears most often in big1.txt and "
                                    "roughly how often.")
        self.compact("C1")

        self.release("T4")
        self.wait_results("T4")
        rec = self.turn("U3-change", "Change of plans on the jobs:\n- Cancel T3, I no longer need it.\n"
                                     "- T5 must be redone with a new input; discard whatever the old T5 returns. New T5: "
                        + self.job("T5", "T5b") + "\n- Add two more jobs, same rules as before:\n- " + self.job("T7")
                        + "\n- " + self.job("T8"))
        self.instruct("cancel", "T3", rec["t0"])
        self.instruct("supersede", "T5", rec["t0"], key="T5b")
        self.instruct("introduce", "T7", rec["t0"])
        self.instruct("introduce", "T8", rec["t0"])
        self.settle("U3")
        self.release("T3", "T5")
        self.wait_results("T3", "T5")

        self.turn("U4-distraction", f"Another unrelated one: cat {WORK}/big3.txt in full and give me the ids (like 3-0123) "
                                    "of the first five lines that contain both 'alpha' and 'zulu'.",
                  during=lambda: self.release("T6", "T7"))
        self.wait_results("T6", "T7")
        self.compact("C2")
        self.checkpoint("CP-A")

        steer = ("Also, on the jobs: add T9 and T10, same rules:\n- " + self.job("T9") + "\n- " + self.job("T10")
                 + "\nAnd cancel T8, I no longer need it.")
        rec = self.turn("U5-distraction+steer", f"cat {WORK}/big4.txt in full and tell me how many lines start with "
                                                "'4-01' and which word is most common on those lines.", steer=steer)
        for t in ("T9", "T10"):
            self.instruct("introduce", t, rec["instruction_t"])
        self.instruct("cancel", "T8", rec["instruction_t"])
        self.settle("U5")
        self.release("T8")
        self.wait_results("T8")
        self.compact("C3")

        self.release("T5b", "T9")
        self.wait_results("T5b", "T9")
        self.turn("U6-distraction", f"Write a small Python script at {WORK / self.salt / 'wordfreq.py'} that prints the ten "
                                    "most common words across big1.txt to big4.txt with counts, run it, and show me the output.")
        self.compact("C4")
        self.checkpoint("CP-B")

        self.release("T10")
        self.wait_results("T10")
        self.settle("drain")
        self.turn("U7-updates", "Any updates on the jobs? Short answer.")
        self.compact("C5")
        self.checkpoint("CP-final")

    def dispatch_more(self, label, intro, adds=(), cancels=(), redefine=None, steer_into=None):
        """One user message that changes the job list; returns its turn record."""
        lines = [intro]
        lines += [f"- Cancel {t}, I no longer need it." for t in cancels]
        if redefine:
            old, new = redefine
            lines.append(f"- {old} must be redone with a new input; discard whatever the old {old} returns. New {old}: " + self.job(old, new))
        if adds:
            lines.append("- Add these jobs, same rules as before:")
            lines += [f"- {self.job(t)}" for t in adds]
        text = "\n".join(lines)
        if steer_into:
            rec = self.turn(steer_into[0], steer_into[1], steer=text, during=steer_into[2] if len(steer_into) > 2 else None)
            t = rec["instruction_t"]
        else:
            rec = self.turn(label, text)
            t = rec["t0"]
        for c in cancels:
            self.instruct("cancel", c, t)
        if redefine:
            self.instruct("supersede", redefine[0], t, key=redefine[1])
        for a in adds:
            self.instruct("introduce", a, t)
        self.settle(label)
        return rec

    def extra_state(self):
        return {}

    def finish(self, error=None):
        self.update()
        for k in self.tokens:
            gate = WORK / "gates" / self.tokens[k]
            if not gate.exists():
                gate.write_text(self.codes[k])
        data = {"arm": self.arm, "effort": self.effort, "model": self.model, "thread": self.tid, "salt": self.salt,
                "started": self.started, "ended": time.time(), "error": error, "tokens": self.tokens, "codes": self.codes,
                "events": self.events, "turns": self.turns, "checkpoints": self.checkpoints, "children": self.children,
                "results": self.results, "parent_turns": self.parent_turns, "tracker_calls": self.tracker_calls,
                "hooks": self.hooks, "parent_gate": self.parent_gate, "child_msgs": self.child_msgs,
                "compaction_times": self.compaction_times, **self.extra_state()}
        (self.out / "run.json").write_text(json.dumps(data, indent=1))
        time.sleep(5)
        self.s.close()
        subprocess.run(["pkill", "-f", f"gate.py T[0-9b]*-{self.salt}"], capture_output=True)


def corpus_windows(lines_per_window=2000):
    """A dump of this repository's own source files (copied, never edited in place) for the orchestrator to review between
    job updates, cut into equal windows so every review turn adds a similar amount of context."""
    dump = WORK / "corpus" / "all-sources.txt"
    if not dump.exists():
        dump.parent.mkdir(parents=True, exist_ok=True)
        repo = pathlib.Path(__file__).resolve().parents[2]
        files = (sorted((repo / "tracker").glob("*.py")) + sorted((repo / "eval").glob("*.py")) + [repo / "tests" / "test_tracker.py"]
                 + [repo / "web" / "app.js", repo / "web" / "app.css"])
        dump.write_text("".join(f"### FILE: {f.parent.name}/{f.name}\n{f.read_text(errors='replace')}\n" for f in files))
    n = len(dump.read_text(errors="replace").splitlines())
    return [(dump, a, min(n, a + lines_per_window - 1)) for a in range(1, n + 1, lines_per_window)]


class ScaledRun(Run):
    """Real scale: auto-compaction at the threshold measured in a busy real orchestrator chat (its last requests
    before each compaction carried 217k-245k input tokens), context filled with real code review between job updates,
    16 jobs over six compactions, results that must survive up to six compactions."""
    TASK_IDS = [f"J{i}" for i in range(1, 17)]
    REDEFINED = ["J4b", "J11b"]
    # tool_output_token_limit: one 2000-line review window is about 22k tokens, so one command can return it whole
    # (model calls can take about a minute each, so fewer calls per review turn matter).
    EXTRA_CONFIG = ["model_auto_compact_token_limit=240000", "tool_output_token_limit=40000"]

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.windows = corpus_windows()
        self.window_i = 0

    def review_prompt(self):
        f, a, b = self.windows[self.window_i % len(self.windows)]
        self.window_i += 1
        return (f"Unrelated review work while the jobs run: print lines {a}-{b} of {f} in full with one sed -n command (set "
                "max_output_tokens to 30000 so nothing is truncated; the file is a dump of several source files, each starting "
                "with '### FILE:'), then list the five most important functions or classes in that range, one line each. Do not "
                "change any files.")

    def fill(self, k, release=(), steer=None):
        """Review work until the thread has auto-compacted k times. Jobs in release finish during the first review turn."""
        first = True
        while self.compactions() < k:
            during = (lambda: self.release(*release)) if (first and release) else None
            label = f"fill-C{k}-{self.window_i}"
            if first and steer:
                self.dispatch_more(steer[0], steer[1], steer_into=(label, self.review_prompt(), during), **steer[2])
            else:
                self.turn(label, self.review_prompt(), during=during)
            first = False
        self.events.append({"t": time.time(), "kind": "cycle_end", "label": f"C{k}", "compactions": self.compactions()})
        self.log(f"compactions so far: {self.compactions()}")

    def script(self):
        intro = ("I'm going to hand you background jobs while we keep working on other things. Delegate each job to its "
                 "own subagent, passing the job text verbatim, and don't wait for them: end your turn once they are "
                 "dispatched. Keep track of them; I'll ask for status now and then.\n\n")
        rec = self.turn("U1-dispatch", intro + "\n".join(f"- {self.job(t)}" for t in self.TASK_IDS[:6]))
        for t in self.TASK_IDS[:6]:
            self.instruct("introduce", t, rec["t0"])
        self.settle("U1")
        self.log(f"U1 spawned {len(self.children)} subagent(s)")
        self.fill(1, release=("J1", "J2"))
        self.dispatch_more("U-change1", "Change of plans on the jobs:", adds=("J7", "J8", "J9"), cancels=("J3",), redefine=("J4", "J4b"))
        self.release("J5")
        self.fill(2, release=("J3", "J4", "J6", "J7"))
        self.checkpoint("CP-A")
        self.dispatch_more("U-change2", "More on the jobs:", adds=("J10", "J11", "J12"), cancels=("J9",))
        self.fill(3, release=("J8", "J4b", "J10", "J9"))
        self.dispatch_more("U-change3", "Job changes:", adds=("J13", "J14"), redefine=("J11", "J11b"))
        self.fill(4, release=("J11", "J12", "J13"))
        self.checkpoint("CP-B")
        self.fill(5, release=("J11b", "J14"),
                  steer=("U-change4", "Also, on the jobs:", {"adds": ("J15", "J16"), "cancels": ("J14",)}))
        self.release("J15", "J16")
        self.wait_results("J15", "J16")
        self.fill(6)
        self.settle("drain")
        self.turn("U-updates", "Any updates on the jobs? Short answer.")
        self.checkpoint("CP-final")


BACKLOG_SLEEPS = {1: (5, 5, 5), 2: (10, 10, 10), 3: (20, 20, 20), 4: (30, 30, 30), 5: (0, 60, 60), 6: (300, 300, 300),
                  7: (400, 400, 400), 8: (500, 500, 500), 9: (700, 700, 0), 10: (900, 900, 900)}
BLOCKED = {"T13": "feed.cfg", "T27": "schema.json"}


class BacklogRun(ScaledRun):
    """Backlog test: the orchestrator owns a 30-task board, hands 3 tasks each to 10 subagents that finish anywhere from
    seconds to ~45 minutes (two hit missing inputs), keeps doing other work, absorbs dropped/added tasks and a retry, and
    reports status after compactions 2, 4 and 5. After compaction 5 it must answer questions that need the decision,
    issue and artifact records. Five auto-compactions at the measured 240k threshold."""
    TASK_IDS, REDEFINED, SLOTS = [], [], 14
    STOP_ON_FAILED_TURN = True

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.dir = WORK / self.salt
        for d in ("inputs", "out", "docs"):
            (self.dir / d).mkdir(parents=True, exist_ok=True)
        self.tasks = {}
        for k, sleeps in BACKLOG_SLEEPS.items():
            for j, secs in enumerate(sleeps):
                self.tasks[f"T{3 * (k - 1) + j + 1:02d}"] = secs
        self.board_snaps, self.answers = [], {}
        self.log(f"backlog dir {self.dir}")

    def task_text(self, tid, secs=None):
        if tid in BLOCKED:
            src = self.dir / "inputs" / BLOCKED[tid]
            return (f"{tid}: copy {src} to {self.dir / 'out' / (tid + '.' + BLOCKED[tid].split('.')[-1])} with cp "
                    "(if the copy fails, report the exact error)")
        secs = self.tasks.get(tid, 30) if secs is None else secs
        return (f"{tid}: run sleep {secs}, then write the first 8 hex chars of sha256('{tid}-{self.salt}') to "
                f"{self.dir / 'out' / (tid + '.txt')}")

    def board(self, label):
        import urllib.request
        try:
            with urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:8795/api/sessions/{self.tid}",
                                                               headers={"X-Actor": "user"}), timeout=60) as r:
                v = json.load(r)
            todos = [{k: t.get(k) for k in ("id", "title", "state", "notes", "created_by")} for t in v["todos"]]
        except Exception as e:
            todos = {"error": str(e)}
        self.board_snaps.append({"label": label, "t": time.time(), "todos": todos})

    def ask(self, label, text):
        rec = self.turn(label, text)
        self.answers[label] = {"t0": rec["t0"], "t1": rec["t1"], "reply": rec["reply"]}

    def checkpoint(self, label):
        self.board(label)
        self.wait_parent_idle()
        rec = self.turn(label, CHECKPOINT_BACKLOG)
        self.checkpoints.append({"label": label, "t0": rec["t0"], "t1": rec["t1"], "reply": rec["reply"]})

    def extra_state(self):
        files = {f.name: f.stat().st_mtime for f in (self.dir / "out").iterdir()} if (self.dir / "out").exists() else {}
        return {"tasks": self.tasks, "dir": str(self.dir), "board_snaps": self.board_snaps, "answers": self.answers, "out_files": files}

    def script(self):
        ids = sorted(self.tasks)
        rec = self.turn("U1-setup", "We're starting a data-prep project. Decision for the record: every output goes under "
                        f"{self.dir / 'out'} as plain .txt files, not JSON, because the downstream loader only reads text. "
                        "Here are the 30 tasks; track all of them for me. Don't start them yet.\n"
                        + "\n".join(f"- {self.task_text(t)}" for t in ids))
        for t in ids:
            self.instruct("introduce", t, rec["t0"])
        self.turn("U2-plan", f"Write a one-page HTML plan for this project to {self.dir / 'docs' / 'plan.html'}: the goal, three "
                             "milestones (M1 setup checks, M2 bulk generation, M3 verification) and the task list grouped by "
                             "milestone. Keep it short.")
        self.turn("U3-dispatch", "Now delegate: spin up 10 subagents and give each 3 of the tasks (T01-T03 to the first, T04-T06 to "
                                 "the second, and so on up to T28-T30). Pass each task text verbatim. Tell each subagent to do its tasks "
                                 "in order and finish with one line per task: 'Txx DONE' or 'Txx BLOCKED: <error>'. Don't wait for them; "
                                 "keep working with me.")
        self.wait_parent_idle()
        self.log(f"U3 spawned {len(self.children)} subagent(s)")
        self.fill(1)
        rec = self.turn("U4-drop", "T21 and T22 are no longer needed; drop them (if their subagent hasn't done them, tell it to skip them).")
        for t in ("T21", "T22"):
            self.instruct("cancel", t, rec["t0"])
        self.fill(2)
        self.checkpoint("CP-A")
        new = {f"T{n}": 45 for n in (31, 32, 33)}
        self.tasks.update(new)
        rec = self.turn("U5-add", "Add three tasks and give them to a new subagent, same reporting rules:\n"
                        + "\n".join(f"- {self.task_text(t)}" for t in sorted(new)))
        for t in sorted(new):
            self.instruct("introduce", t, rec["t0"])
        self.fill(3)
        (self.dir / "inputs" / "feed.cfg").write_text("feed=ok\n")
        rec = self.turn("U6-retry", f"I've put feed.cfg in place at {self.dir / 'inputs' / 'feed.cfg'}. Retry T13.")
        self.instruct("retry", "T13", rec["t0"])
        self.fill(4)
        self.checkpoint("CP-B")
        self.fill(5)
        self.settle("drain")
        self.checkpoint("CP-final")
        for k, q in RECALL:
            self.ask(k, q)

    def tail(self):
        done = {c["label"] for c in self.checkpoints} | set(self.answers)
        steps = [("CP-final", lambda: self.checkpoint("CP-final"))] + [(k, (lambda k=k, q=q: self.ask(k, q))) for k, q in RECALL]
        return [(k, f) for k, f in steps if k not in done]


RECALL = (("Q-decision", "Quick question: what did we decide about the output files, and why?"),
          ("Q-issue", "Earlier some tasks failed on missing inputs. Which tasks, what exactly was missing, and is each one resolved now?"),
          ("Q-artifact", "Where is the plan report you wrote at the start, and what is its first milestone?"))
CHECKPOINT_BACKLOG = ("Status check on the project tasks. Reply with only a JSON code block, no other text, listing every task I have "
                      "given you (T01 onward), including dropped ones:\n" + FENCE + "json\n"
                      '{"tasks": [{"id": "T01", "state": "done | blocked | pending | dropped"}]}\n' + FENCE
                      + "\ndone = its subagent reported it DONE; blocked = reported BLOCKED and not yet fixed; pending = not reported yet; "
                      "dropped = I dropped it. Check whatever you need to before answering.")
BSTATE = {"done": "done", "completed": "done", "complete": "done", "finished": "done",
          "blocked": "blocked", "failed": "blocked", "error": "blocked",
          "pending": "pending", "in_progress": "pending", "running": "pending", "assigned": "pending", "waiting": "pending",
          "not_started": "pending", "queued": "pending", "todo": "pending", "retrying": "pending",
          "dropped": "dropped", "cancelled": "dropped", "canceled": "dropped", "removed": "dropped", "skipped": "dropped"}
REPORT = re.compile(r"\b(T\d{2})\b[^A-Za-z0-9\n]{0,4}(DONE|BLOCKED|SKIPPED)", re.I)


def backlog_truth(run, t0, t1):
    ev = run["events"]
    intro = [e["task"] for e in ev if e["kind"] == "introduce" and e["t"] <= t0]
    dropped = {e["task"] for e in ev if e["kind"] == "cancel" and e["t"] <= t0}
    retry_t = next((e["t"] for e in ev if e["kind"] == "retry" and e["t"] <= t0), None)
    reports = {}
    for m in sorted(run["child_msgs"], key=lambda x: x["t"]):
        for tid, verdict in REPORT.findall(m["text"]):
            reports.setdefault(tid.upper(), []).append((m["t"], verdict.upper()))
    exp = {}
    for t in intro:
        if t in dropped:
            exp[t] = {"dropped"}
            continue
        hist = reports.get(t, [])
        if t == "T13" and retry_t:
            fixed = [x for x in hist if x[0] > retry_t]
            f = run["out_files"].get("T13.cfg")
            if any(v == "DONE" for ts, v in fixed if ts < t0) or (f and f < t0):
                exp[t] = {"done"}
            else:
                exp[t] = {"pending", "blocked", "done"} if any(t0 <= ts <= t1 for ts, _ in fixed) else {"pending", "blocked"}
            continue
        before = [v for ts, v in hist if ts < t0]
        during = [v for ts, v in hist if t0 <= ts <= t1]
        if before:
            exp[t] = {"done"} if before[-1] == "DONE" else {"blocked"} if before[-1] == "BLOCKED" else {"pending"}
        elif during:
            exp[t] = {"pending", "done" if during[-1] == "DONE" else "blocked"}
        else:
            exp[t] = {"pending"}
    return exp


def score_backlog_checkpoint(run, cp):
    exp = backlog_truth(run, cp["t0"], cp["t1"])
    m = re.search(FENCE + r"(?:json)?\s*(\{.*?\})\s*" + FENCE, cp["reply"] or "", re.S)
    raw = m.group(1) if m else (cp["reply"] or "")[(cp["reply"] or "").find("{"):(cp["reply"] or "").rfind("}") + 1]
    try:
        got = {str(x.get("id", "")).upper(): BSTATE.get(str(x.get("state", "")).lower().replace(" ", "_"), str(x.get("state")))
               for x in json.loads(raw).get("tasks", [])}
    except (ValueError, AttributeError):
        return {"correct": 0, "total": len(exp), "errors": [("*", "no parseable JSON")]}
    errors = [(t, f"expected {sorted(e)} got {got.get(t)}") for t, e in exp.items() if got.get(t) not in e]
    errors += [(t, "unknown task") for t in got if t not in exp]
    return {"correct": len(exp) - sum(1 for t in exp if got.get(t) not in exp[t]), "total": len(exp), "errors": errors}


def score_board(run, snap, cp):
    """How well the tracker's todo board (written by the scribe and the agent) matches reality at a checkpoint."""
    if not isinstance(snap["todos"], list):
        return None
    exp = backlog_truth(run, cp["t0"], cp["t1"])
    by = {}
    for td in snap["todos"]:
        for tid in re.findall(r"\bT\d{2}\b", td["title"] or ""):
            by.setdefault(tid, []).append(td)
    ok, errs = 0, []
    for t, e in exp.items():
        tds = by.get(t, [])
        if "dropped" in e:
            good = not tds or all(re.search(r"drop|cancel|no longer", (x["notes"] or "") + x["title"], re.I) for x in tds)
        elif not tds:
            good = False
        else:
            st = tds[-1]["state"]
            mapped = "done" if st == "done" else "blocked" if st == "blocked" else "pending"
            good = mapped in e
        ok += good
        if not good:
            errs.append((t, sorted(e), [x["state"] for x in tds] or "no todo"))
    return {"correct": ok, "total": len(exp), "errors": errs}


def score_recall(run):
    a = {k: (v.get("reply") or "") for k, v in run.get("answers", {}).items()}
    d, i, r = a.get("Q-decision", ""), a.get("Q-issue", ""), a.get("Q-artifact", "")
    return {
        "decision": bool(re.search(r"\.txt|plain text|text files", d, re.I) and re.search(r"loader", d, re.I)),
        "issue": sum(bool(re.search(x, i, re.I)) for x in (r"T13", r"T27", r"feed\.cfg", r"schema\.json")) / 4,
        "artifact": bool(run["dir"] + "/docs/plan.html" in r and re.search(r"M1|setup check", r, re.I)),
    }


def tracker_usage(run):
    """session_tracker tool calls by the orchestrator, per compaction cycle (0 = before the first compaction)."""
    cuts = sorted(run.get("compaction_times") or [])
    per = {}
    for c in run["tracker_calls"]:
        if "session_tracker" not in (c.get("server") or ""):
            continue
        cyc = sum(1 for x in cuts if x <= c["t"])
        per.setdefault(cyc, {}).setdefault(c.get("tool"), 0)
        per[cyc][c.get("tool")] += 1
    return per


def score_backlog(paths):
    for p in paths:
        run = json.loads((pathlib.Path(p) / "run.json").read_text())
        snaps = {s["label"]: s for s in run.get("board_snaps", [])}
        row = {"arm": run["arm"], "error": run.get("error"), "compactions": len(run.get("compaction_times") or []),
               "checkpoints": {}, "board": {}, "recall": score_recall(run), "tracker_calls_per_cycle": tracker_usage(run),
               "subagents": len(run["children"]), "minutes": round((run["ended"] - run["started"]) / 60, 1)}
        for cp in run["checkpoints"]:
            row["checkpoints"][cp["label"]] = score_backlog_checkpoint(run, cp)
            if cp["label"] in snaps:
                row["board"][cp["label"]] = score_board(run, snaps[cp["label"]], cp)
        (pathlib.Path(p) / "score.json").write_text(json.dumps(row, indent=1))
        print(json.dumps({k: (v if k not in ("checkpoints", "board") else {x: f"{y['correct']}/{y['total']}" if y else None for x, y in v.items()})
                          for k, v in row.items()}))
        for k, v in row["checkpoints"].items():
            for e in v["errors"]:
                print(f"    report {k}: {e}")
        for k, v in row["board"].items():
            for e in (v or {}).get("errors", []):
                print(f"    board {k}: {e}")


def parse_jobs(reply):
    if not reply:
        return None
    m = re.search(FENCE + r"(?:json)?\s*(\{.*?\})\s*" + FENCE, reply, re.S)
    raw = m.group(1) if m else reply[reply.find("{"):reply.rfind("}") + 1]
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    jobs = data.get("jobs") or data.get("tasks") or []
    out = {}
    for j in jobs:
        jid = str(j.get("id", "")).strip().upper()
        state = STATES.get(str(j.get("state", "")).strip().lower().replace("-", "_"), str(j.get("state")))
        out[jid] = {"state": state, "result": (str(j["result"]).strip().lower() if j.get("result") else None)}
    return out


def expected(run, cp):
    t0, t1 = cp["t0"], cp["t1"]
    ev = [e for e in run["events"] if e["t"] is not None and e["t"] <= t0]
    intro = [e["task"] for e in ev if e["kind"] == "introduce"]
    cancelled = {e["task"] for e in ev if e["kind"] == "cancel"}
    current = {t: t for t in intro}
    for e in ev:
        if e["kind"] == "supersede":
            current[e["task"]] = e["key"]
    exp = {}
    for t in intro:
        key = current[t]
        res = run["results"].get(key)
        code = run["codes"][key]
        if t in cancelled:
            exp[t] = {"allowed": ["cancelled"], "code": None, "key": key}
        elif res and res["t"] < t0:
            exp[t] = {"allowed": ["done"], "code": code, "key": key}
        elif res and res["t"] <= t1:
            exp[t] = {"allowed": ["done", "running"], "code": code, "key": key}
        else:
            exp[t] = {"allowed": ["running", "not_started"], "code": code, "key": key}
    return exp


def score_checkpoint(run, cp):
    exp = expected(run, cp)
    got = parse_jobs(cp["reply"])
    errors = []
    if got is None:
        return {"correct": 0, "total": len(exp), "errors": [{"task": "*", "error": "no parseable JSON"}]}
    correct = 0
    old_codes = {run["codes"][k]: k for k in run["codes"]}
    for t, e in exp.items():
        g = got.get(t)
        if not g:
            errors.append({"task": t, "error": "missing", "expected": e["allowed"]})
            continue
        if g["state"] not in e["allowed"]:
            errors.append({"task": t, "error": "wrong state", "expected": e["allowed"], "got": g["state"]})
            continue
        if g["state"] == "done" and g["result"] != e["code"]:
            src = old_codes.get(g["result"] or "")
            errors.append({"task": t, "error": "wrong result", "got": g["result"], "matches": src})
            continue
        correct += 1
    for t in got:
        if t not in exp:
            errors.append({"task": t, "error": "unknown job reported", "got": got[t]["state"]})
    return {"correct": correct, "total": len(exp), "errors": errors}


def behavior(run):
    ev = run["events"]
    intro = [e["task"] for e in ev if e["kind"] == "introduce"]
    cancel_t = {e["task"]: e["t"] for e in ev if e["kind"] == "cancel"}
    current = {t: t for t in intro}
    for e in ev:
        if e["kind"] == "supersede":
            current[e["task"]] = e["key"]
    holders = {}
    for c, i in run["children"].items():
        for k in i["tokens"]:
            holders.setdefault(k, []).append(c)
    must_run = [current[t] for t in intro if t not in cancel_t]
    released = {e["key"]: e["t"] for e in ev if e["kind"] == "release"}
    return {
        "children": len(run["children"]),
        "never_run": [k for k in must_run if k not in holders],
        "duplicate_spawns": {k: len(v) for k, v in holders.items() if len(v) > 1},
        "cancel_not_enforced": [t for t in cancel_t if t in run["results"] and t in released],
        "orchestrator_ran_job_itself": sorted({g["key"] for g in run["parent_gate"]}),
        "unprompted_turns": max(0, len(run["parent_turns"]) - len(run["turns"]) - sum(1 for e in ev if e["kind"] == "compact")),
        "tracker_tool_calls": sum(1 for c in run["tracker_calls"] if "session_tracker" in (c.get("server") or "")),
        "tracker_injections": sum(1 for h in run["hooks"] if h["thread"] == run["thread"]
                                  and any("session-tracker" in (e.get("text") or "") for e in h["entries"])),
        "compactions_ok": sum(1 for e in ev if e["kind"] == "compact" and e["ok"]),
        "minutes": round((run["ended"] - run["started"]) / 60, 1),
    }


def score(paths):
    rows = []
    for p in paths:
        f = pathlib.Path(p) / "run.json"
        if not f.exists():
            continue
        run = json.loads(f.read_text())
        cps = {cp["label"]: score_checkpoint(run, cp) for cp in run["checkpoints"]}
        correct = sum(c["correct"] for c in cps.values())
        total = sum(c["total"] for c in cps.values())
        row = {"run": str(p), "arm": run["arm"], "effort": run["effort"], "error": run.get("error"),
               "accuracy": f"{correct}/{total}", "checkpoints": cps, "behavior": behavior(run)}
        (pathlib.Path(p) / "score.json").write_text(json.dumps(row, indent=1))
        rows.append(row)
    for r in rows:
        cp = "  ".join(f"{k} {v['correct']}/{v['total']}" for k, v in r["checkpoints"].items())
        b = r["behavior"]
        print(f"{r['arm']:8} {r['effort']:7} {r['accuracy']:6} | {cp} | never_run {b['never_run']} dup {b['duplicate_spawns']} "
              f"tracker calls {b['tracker_tool_calls']} injections {b['tracker_injections']} | {b['minutes']} min | {r['run']}")
        for k, v in r["checkpoints"].items():
            for e in v["errors"]:
                print(f"    {k}: {e}")
    return rows


def main(argv):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("arm", choices=["tracker", "control"])
    r.add_argument("out")
    r.add_argument("--effort", default=os.environ.get("ST_E2E_EFFORT", "medium"))
    r.add_argument("--model", default=MODEL)
    r.add_argument("--scenario", choices=["basic", "scaled", "backlog"], default="basic")
    rs = sub.add_parser("resume")
    rs.add_argument("out")
    s = sub.add_parser("score")
    s.add_argument("outs", nargs="+")
    s.add_argument("--backlog", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "score":
        (score_backlog if a.backlog else score)(a.outs)
        return 0
    if a.cmd == "resume":
        run = BacklogRun.resume(pathlib.Path(a.out))
        try:
            for _, step in run.tail():
                step()
            run.finish()
        except BaseException as e:
            run.finish(error=repr(e))
            raise
        score_backlog([a.out])
        return 0
    run = {"scaled": ScaledRun, "backlog": BacklogRun}.get(a.scenario, Run)(a.arm, pathlib.Path(a.out), a.effort, a.model)
    try:
        run.script()
        run.finish()
    except BaseException as e:
        run.finish(error=repr(e))
        raise
    (score_backlog if a.scenario == "backlog" else score)([a.out])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

