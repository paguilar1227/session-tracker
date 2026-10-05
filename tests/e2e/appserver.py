"""Minimal client for 'codex app-server' (newline-delimited JSON-RPC over stdio), for driving real long-lived threads in
tests: several user turns in one live thread, subagents that outlive a turn, steering and manual compaction."""
import itertools
import json
import os
import queue
import subprocess
import threading
import time


class AppServer:
    def __init__(self, log_path, config=(), env=None, cwd=None, stderr_path=None):
        args = ["codex", "app-server"]
        for c in config:
            args += ["-c", c]
        self.log = open(log_path, "a", buffering=1)
        self.proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=open(stderr_path, 'a') if stderr_path else subprocess.DEVNULL,
                                     text=True, bufsize=1, env=dict(os.environ, **(env or {})), cwd=cwd)
        self.ids = itertools.count(1)
        self.pending = {}
        self.lock = threading.Lock()
        self.events = queue.Queue()
        self.notes = []          # every notification, in order
        self.cond = threading.Condition()
        threading.Thread(target=self._read, daemon=True).start()

    def _write(self, msg):
        self.log.write(json.dumps({"t": time.time(), "out": msg}) + "\n")
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def _read(self):
        for line in self.proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            self.log.write(json.dumps({"t": time.time(), "in": msg}) + "\n")
            if "id" in msg and ("result" in msg or "error" in msg):
                with self.lock:
                    slot = self.pending.pop(msg["id"], None)
                if slot:
                    slot.put(msg)
            elif "id" in msg and "method" in msg:
                self._answer(msg)
            elif "method" in msg:
                msg["_t"] = time.time()
                with self.cond:
                    self.notes.append(msg)
                    self.cond.notify_all()
        with self.cond:
            self.notes.append({"method": "__closed__"})
            self.cond.notify_all()

    def _answer(self, req):
        """Server-to-client requests. Approval policy is 'never', so these are rare; refuse anything interactive."""
        method = req["method"]
        if method.endswith("requestApproval") or method in ("execCommandApproval", "applyPatchApproval"):
            result = {"decision": "approved"}
        elif method == "mcpServer/elicitation/request":
            result = {"action": "decline"}
        else:
            self._write({"id": req["id"], "error": {"code": -32601, "message": f"test client does not handle {method}"}})
            return
        self._write({"id": req["id"], "result": result})

    def request(self, method, params=None, timeout=None):
        rid = next(self.ids)
        slot = queue.Queue()
        with self.lock:
            self.pending[rid] = slot
        self._write({"id": rid, "method": method, "params": params or {}})
        msg = slot.get(timeout=timeout)
        if "error" in msg:
            raise RuntimeError(f"{method}: {msg['error']}")
        return msg["result"]

    def notify(self, method, params=None):
        self._write({"method": method, **({"params": params} if params is not None else {})})

    def initialize(self):
        self.request("initialize", {"clientInfo": {"name": "session-tracker-e2e", "version": "1"}})
        self.notify("initialized")

    def wait_for(self, pred, start=0):
        """Block until a notification at index >= start satisfies pred; returns (index, note)."""
        with self.cond:
            i = start
            while True:
                while i < len(self.notes):
                    n = self.notes[i]
                    if n.get("method") == "__closed__":
                        raise RuntimeError("app-server exited")
                    if pred(n):
                        return i, n
                    i += 1
                self.cond.wait()

    def mark(self):
        with self.cond:
            return len(self.notes)

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=30)
        except Exception:
            self.proc.kill()
