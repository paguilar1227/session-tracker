"""Ledger extraction: each finished turn -> Claude Sonnet 5.5 (Copilot chat completions)."""
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request

from . import artifacts, config, db, delta, projects, store
from .codex_source import CodexSource

log = logging.getLogger("tracker.extractor")
PROMPT_VERSION = "scribe-v7"

SYSTEM = """You are the scribe for one coding-agent session. You watch what the main agent (the model running the session) did in ONE turn and keep its durable ledger. You receive: the session's existing ledger, open todos (each with an id), its subagents (task, status, result) and its artifacts folder; a block of facts computed from Codex's records of the turn (files read, files changed, commands, tests and checks run, subagents) — these facts are reliable and are NOT the agent's own words, but they are a lower bound: checks run inside scripts, browsers or other tools may not be itemized, so never discount a reported result or skip recording it only because the facts block does not list it; and the trimmed activity of the turn (user messages, agent messages, commands with exit codes, file changes, tool calls; each line starts with an item id in brackets).

Your jobs: (a) record what was decided and why; (b) notice when the main agent made a mistake and write it down as a lesson, so that a future session reading only your lesson does not make the same mistake again; (c) keep the session's task board, issues and artifact list current, so the agent can rely on them instead of its memory.

Return ONE JSON object and nothing else:
{
  "decisions": [{"title": str, "choice": str, "rationale": str, "alternatives_rejected": [str], "made_by": "user"|"agent"|"reviewer_or_parent"|"unknown", "status": "decided"|"proposed"|"superseded", "evidence": [item ids]}],
  "mistakes": [{"title": str, "what_happened": str, "why_it_was_a_mistake": str, "lesson": str, "repeat_of": int|null, "evidence": [item ids]}],
  "corrections": [{"mistake_id": int, "cause": str, "lesson": str, "why": str, "evidence": [item ids]}],
  "issues": [{"title": str, "status": "open"|"resolved", "detail": str, "evidence": [item ids]}],
  "todos": [{"title": str, "owner": "user"|"agent"|"parent"|"unknown", "priority": "P1"|"P2"|"P3", "state": "not_started"|"in_progress"|"waiting"|"blocked", "note": str, "evidence": [item ids]}],
  "updates": [{"ledger_id": int, "status": str, "note": str} | {"todo_id": int, "state": "not_started"|"in_progress"|"review"|"blocked"|"waiting"|"done", "note": str}],
  "drop": [{"todo_id": int, "reason": str, "evidence": [item ids]}],
  "artifacts": [{"path": str, "title": str, "evidence": [item ids]}]
}

Decisions:
- A decision is a choice between alternatives that shapes future work. Mark it "proposed" if it is only a recommendation awaiting someone's approval; "decided" only if it was actually adopted or acted on in this turn.

Mistakes (the main agent's own errors):
- A mistake is something the main agent did wrong that cost time, produced a wrong or unverified result, or had to be corrected. Examples: a wrong assumption; misreading or ignoring the user's instructions or constraints; a command, edit or script that broke because of how the agent wrote it; claiming success, verification or completeness that the turn does not support; doing work outside the requested scope; stopping to ask the user for approval, confirmation or review that they had already given or that the task did not need, instead of proceeding; an approach the agent abandoned after it failed because it was a poor choice; repeating something that already failed.
- Strong signals: the user correcting, rebuking or pushing back on the agent; the agent saying it was wrong, reverting or redoing its own work; an error whose cause is the agent's own command or code.
- When the agent admits, or the turn shows, a gap or error in its own earlier work (this turn or an earlier one) and then fixes it, record the gap as a mistake. Recording the fix as a decision is not enough.
- Record each distinct mistake separately; one turn often holds several (for example an unsupported claim and, separately, the earlier design gap the agent is now fixing).
- Check every USER line first. A user message that corrects or rebukes the agent points to a mistake even when the behavior it corrects happened in an earlier turn and the rest of this turn is unrelated work; record that mistake from what the user and agent say about it.
- Check the agent's claims against the facts block. Record a mistake titled "Claimed ... without ..." only when all of these hold: the agent stated as fact to the user or another agent that something is done, fixed, verified, tested, reviewed or working; the facts block or activity shows the specific check that claim needs did not happen in this turn (for example it read one of three files but called all three solid, said tests pass when no test ran, or called a deploy fixed after testing only locally); and the claim was relied on (work continued, was pushed or handed over on it). Do not flag reports of what the agent observed, hypotheses, plans, or status updates, and do not flag claims about work that earlier turns may have checked.
- NOT mistakes: failures caused by the environment or outside services (network, outages, rate limits, missing credits or permissions the agent could not know about) — record those as issues if they matter. Also not mistakes: intentional stops (e.g. Ctrl-C of a dev server), blocking receipts that exit non-zero on purpose, benign pipeline exit codes where the needed output was still obtained, normal exploration that simply found nothing, and the user simply changing their mind or rejecting a proposal whose trade-offs the agent had stated. Do not call an action a mistake just because nobody asked for it (for example a commit or an extra check) unless the user's instructions or the turn show it was unwanted or harmful, and do not call a statement false or premature when the turn ends before it could be confirmed or refuted.
- "what_happened": one or two sentences of fact.
- "why_it_was_a_mistake": the root cause, not the symptom. Find the agent's own command, code or message that went wrong and name the specific part of it that caused the problem (quote it if short). An error message often describes a downstream symptom; trace it back to what the agent wrote. For an ignored instruction or out-of-scope work, the root cause is what led the agent to override the instruction (a default habit, an assumption, a misread phrase), not a restatement of the violation. If the evidence does not show the cause, say what is known and that the cause is unconfirmed.
- "lesson" is the most important field. Write it TO a future session as a direct instruction that names the earliest check or habit that would have prevented this mistake before it reached the user, production or another agent — not only the repair applied afterwards (for example "Build and run the container image locally before pushing a Dockerfile change." rather than "Add the missing COPY line."). Be specific (tools, commands, flags, file names) and general enough to apply next time. One or two sentences.
- If the existing ledger already holds the same lesson, do not add a duplicate. If the agent repeated an earlier mistake anyway, record it again with repeat_of set to that entry's id and make the lesson firmer.

Corrections:
- Existing mistakes are listed with their cause and lesson. Recorded mistakes are never edited. If this turn shows that a recorded cause or lesson was wrong or incomplete (for example the real cause came out later), add a correction with the mistake's id, the corrected cause and lesson, and why. Do not add corrections just to reword.

Issues, todos, updates:
- Issues are the triage ledger: problems in the work or its environment that are not the agent's own mistake: blockers, failing checks, review findings, bugs found, and environment or tool failures. Record them when found (open) and when fixed or cleared (resolved), e.g. a reviewer's FAIL that was addressed, or a blocker that a re-check now passes. Record the resolved issue even if the problem was never recorded as open; the existing ledger may be incomplete. In "detail" give what is known of: the symptom (exact error text), the cause or current hypothesis (say which), how to reproduce, and the workaround or fix. When the environment or a tool failed and the agent worked around it, record a resolved issue with the workaround, so the next session can go straight to it. When this turn advances an existing issue (new hypothesis, confirmed cause, fix, re-check), add an update with its id and a "note" (and a status if it changed).
- Todos are the session's task board. Add one for every distinct piece of work the user or the agent identified that is not finished at the end of this turn: requested tasks, steps of a plan the agent is carrying out, follow-ups, and tasks handed to subagents (one todo per task, not one per subagent). Set "state" (in_progress if the agent is working on it, waiting if handed to a subagent or waiting on someone, blocked if stuck) and put who has it or why in "note". Do not add todos for work finished within this turn, and never add one that matches an open todo in meaning, even if worded differently.
- Keep existing todos current with "updates" using their id: state changes (in_progress, waiting, blocked with the reason, review, done) and a short "note" for real progress (e.g. "delegated to /root/j7", the result or outcome when done, the blocker). A subagent's result in the subagents list or activity finishes its todo. Note-only updates are fine; skip notes that add nothing.
- Put a todo in "drop" only when the user or the agent said in this turn that it is no longer needed, cancelled or replaced; it is deleted.
- Never repeat an item that already exists in the ledger or todo list. If this turn changes an existing decision, issue or todo (adopted, superseded, resolved, finished, blocked), put it in "updates" using its id. Decision statuses: proposed, decided, superseded. Issue statuses: open, resolved. Mistakes are permanent; never update them.

Artifacts:
- List deliverables the agent created or substantially rewrote in this turn that someone will want to open later: reports, documents, plans, specs, diagrams, HTML pages, screenshots, data exports. Give the absolute path from the facts block or activity and a short title. Skip source code, configs, tests, logs and scratch files, and anything inside the session's artifacts folder (those are listed automatically).

General:
- Do not record routine steps, successful reads, or restatements of instructions.
- Use only information present in the input. Do not invent. Empty arrays are fine.
- Keep each string concise and specific (names, paths, values) so it is useful out of context."""

RECHECK_SYSTEM = """You re-check ONE mistake that a scribe recorded about a coding agent, against the turn it came from. You receive the recorded mistake (what happened, cause, lesson, and any corrections so far), facts computed from Codex's records of that turn, and the trimmed turn activity.

Decide whether the recorded cause names the real root cause (the specific part of the agent's own command, code or message that caused the problem, not a downstream symptom) and whether the lesson names the earliest check or habit that would have prevented it (not only the repair). If both hold, return {"verdict": "confirmed"}. Otherwise return {"verdict": "corrected", "cause": str, "lesson": str, "why": str, "evidence": [item ids]} with a corrected cause and a lesson written TO a future session as a direct, specific instruction (one or two sentences). If the mistake has no lesson, write one. Use only the evidence given; if it cannot show the cause, keep the recorded cause and say it is unconfirmed. Return JSON only."""


class ExtractError(RuntimeError):
    pass


GITHUB_API = "https://api.github.com"
DEFAULT_COPILOT_BASE = "https://api.githubcopilot.com"


class Copilot:
    """GitHub Copilot's chat API, signed in with the gh CLI's token (the account Copilot is billed to)."""

    def __init__(self):
        self._token = None
        self._base = None
        self._limits = {}

    def token(self, refresh=False):
        if self._token and not refresh:
            return self._token
        gh = shutil.which("gh") or "/opt/homebrew/bin/gh"
        cmd = [gh, "auth", "token"] + (["--user", config.GH_USER] if config.GH_USER else [])
        try:
            self._token = subprocess.check_output(cmd, text=True, timeout=20, stderr=subprocess.PIPE).strip()
        except (OSError, subprocess.SubprocessError) as e:
            detail = (getattr(e, "stderr", None) or str(e)).strip()
            raise ExtractError(f"could not read a GitHub token with 'gh {' '.join(cmd[1:])}': {detail}") from e
        return self._token

    def _github(self, path):
        req = urllib.request.Request(GITHUB_API + path, headers={"Authorization": "Bearer " + self.token(),
                                                                "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            raise ExtractError(f"GitHub HTTP {e.code} for {path}: {e.read().decode(errors='replace')[:200]}") from e
        except (urllib.error.URLError, OSError) as e:
            raise ExtractError(f"GitHub request failed: {e}") from e

    def account(self):
        """The signed-in account's Copilot plan and API endpoints, from GitHub's Copilot account endpoint."""
        return self._github("/copilot_internal/user")

    def base(self):
        if config.COPILOT_BASE:
            return config.COPILOT_BASE
        if not self._base:
            try:
                self._base = ((self.account().get("endpoints") or {}).get("api") or DEFAULT_COPILOT_BASE).rstrip("/")
            except ExtractError as e:
                log.warning("Copilot endpoint discovery failed, using %s: %s", DEFAULT_COPILOT_BASE, e)
                return DEFAULT_COPILOT_BASE
        return self._base

    def _request(self, method, path, body=None, timeout=600):
        for attempt in (0, 1):
            req = urllib.request.Request(self.base() + path, method=method,
                                         data=json.dumps(body).encode() if body is not None else None,
                                         headers={"Authorization": "Bearer " + self.token(refresh=attempt == 1),
                                                  "Copilot-Integration-Id": config.COPILOT_INTEGRATION_ID,
                                                  "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return json.load(r)
            except urllib.error.HTTPError as e:
                detail = e.read().decode(errors="replace")[:500]
                if e.code == 401 and attempt == 0:
                    continue
                raise ExtractError(f"Copilot HTTP {e.code}: {detail}") from e
            except (urllib.error.URLError, OSError) as e:
                raise ExtractError(f"Copilot request failed: {e}") from e

    def chat_models(self):
        """Models this account may use through the chat completions endpoint the scribe calls."""
        out = []
        for m in self._request("GET", "/models", timeout=30).get("data", []):
            usable = ((m.get("capabilities") or {}).get("type") == "chat"
                      and (m.get("policy") or {}).get("state", "enabled") == "enabled"
                      and "/chat/completions" in (m.get("supported_endpoints") or ["/chat/completions"]))
            if usable:
                out.append(m["id"])
        return out

    def check(self, model=None, live=True):
        """Everything the scribe needs, step by step: gh token, Copilot plan, API host, model, and one real call."""
        model = model or config.EXTRACT_MODEL
        report = {"model": model, "ok": False}
        report["login"] = self._github("/user").get("login")
        acct = self.account()
        report["plan"], report["chat_enabled"] = acct.get("copilot_plan"), acct.get("chat_enabled")
        report["base"] = self.base()
        report["models"] = self.chat_models()
        report["model_ok"] = model in report["models"]
        if report["model_ok"] and live:
            text, _, secs = self.chat(model, "Reply with the single word OK.", "Ready?", config.EXTRACT_EFFORT)
            report["reply"], report["seconds"] = text.strip()[:40], round(secs, 1)
        report["ok"] = bool(report["model_ok"] and (not live or report.get("reply")))
        return report

    def max_prompt_tokens(self, model):
        if model not in self._limits:
            try:
                for m in self._request("GET", "/models", timeout=30).get("data", []):
                    limits = (m.get("capabilities") or {}).get("limits") or {}
                    if limits.get("max_prompt_tokens"):
                        self._limits[m["id"]] = limits["max_prompt_tokens"]
            except ExtractError as e:
                log.warning("model limits unavailable: %s", e)
        return self._limits.get(model)

    def chat(self, model, system, user, effort):
        body = {"model": model, "stream": False,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        if effort:
            body["reasoning_effort"] = effort
        t0 = time.monotonic()
        d = self._request("POST", "/chat/completions", body)
        secs = time.monotonic() - t0
        try:
            text = d["choices"][0]["message"].get("content") or ""
        except (KeyError, IndexError) as e:
            raise ExtractError(f"unexpected Copilot response: {json.dumps(d)[:300]}") from e
        return text, d.get("usage"), secs


def parse_json(text):
    t = (text or "").strip()
    fence = re.match(r"^\x60\x60\x60[a-zA-Z]*\n(.*)\n\x60\x60\x60$", t, re.S)
    if fence:
        t = fence.group(1)
    try:
        out = json.loads(t)
    except ValueError:
        start, end = t.find("{"), t.rfind("}")  # prose or a stray fence around the object
        try:
            out = json.loads(t[start:end + 1]) if 0 <= start < end else None
        except ValueError:
            out = None
        if out is None:
            raise ExtractError(f"model did not return JSON: {t[:200]}")
    if not isinstance(out, dict):
        raise ExtractError("model JSON is not an object")
    return out


def context_block(c, session_id):
    ledger = store._decorate(c, db.rows(c.execute("select id, kind, status, title, lesson, rationale from ledger where session_id=? order by id", (session_id,))))
    todos = db.rows(c.execute("select id, state, priority, title, notes from todos where session_id=? and state!='done' order by id", (session_id,)))

    def line(e):
        out = f"- #{e['id']} {e['kind']} [{e['status']}] {e['title']}"
        if e["kind"] == "mistake":
            out += (f" — cause: {e['effective_cause']}" if e.get("effective_cause") else "") + (f" — lesson: {e['effective_lesson']}" if e.get("effective_lesson") else "")
        return out
    lines = ["Existing ledger:"] + ([line(e) for e in ledger] or ["(empty)"])
    lines += ["", "Open todos:"] + ([f"- #{t['id']} [{t['state']}, {t['priority']}] {t['title']}" + (f" — last note: {_last_note(t['notes'])}" if _last_note(t['notes']) else "")
                                    for t in todos] or ["(none)"])
    kids = c.execute("""select agent_path, nickname, status, task, result from sessions where parent_id=? order by created_at""", (session_id,)).fetchall()
    lines += ["", "Subagents:"] + ([f"- {k['agent_path'] or k['nickname'] or '?'} [{k['status']}]" + (f" task: {_clip(k['task'], 300)}" if k['task'] else "")
                                   + (f" — result: {_clip(k['result'], 600)}" if k['result'] and k['status'] in ("done", "failed", "interrupted") else "") for k in kids] or ["(none)"])
    folder = c.execute("select artifacts_dir from sessions where id=?", (session_id,)).fetchone()
    lines += ["", f"Artifacts folder: {folder[0] if folder and folder[0] else '(unknown)'}"]
    return "\n".join(lines)


def _clip(s, n):
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[:n - 1] + "…"


def _last_note(notes):
    parts = [x.strip() for x in (notes or "").split("\n") if x.strip().startswith("- ")]
    return _clip(parts[-1][2:], 160) if parts else ""


def _append_note(c, todo, note, turn_id, actor):
    stamp = time.strftime("%b %d %H:%M")
    notes = (todo["notes"] or "").rstrip()
    store.update_todo(c, todo["id"], {"notes": (notes + "\n\n" if notes else "") + f"- {stamp} · {note.strip()}"}, actor=actor)


def _norm(s):
    return re.sub(r"\W+", " ", (s or "").lower()).strip()


def _evidence(ids, turn_id):
    return [{"turn_id": turn_id, "item": str(i)} for i in (ids or []) if i]


def apply_result(c, session_id, turn_id, result, model):
    """Write extracted items. Returns counts."""
    existing = {(r["kind"], _norm(r["title"])) for r in c.execute("select kind, title from ledger where session_id=?", (session_id,))}
    existing_todos = {_norm(r["title"]) for r in c.execute("select title from todos where session_id=?", (session_id,))}
    counts = {"decision": 0, "mistake": 0, "correction": 0, "issue": 0, "todo": 0, "update": 0, "drop": 0, "artifact": 0}
    meta = {"turn_id": turn_id, "model": model}

    def add(kind, data):
        key = (kind, _norm(data["title"]))
        if not data["title"] or (key in existing and "Repeated mistake" not in (data.get("lesson") or "")):
            return
        existing.add(key)
        store.create_ledger(c, session_id, {**data, **meta, "kind": kind}, source="extractor")
        counts[kind] += 1

    for d in result.get("decisions") or []:
        status = d.get("status") if d.get("status") in ("proposed", "decided", "superseded") else "proposed"
        add("decision", {"title": str(d.get("title") or d.get("choice") or "")[:300], "body": d.get("choice") or "",
                         "rationale": d.get("rationale"), "alternatives": d.get("alternatives_rejected") or [],
                         "made_by": d.get("made_by"), "status": status, "evidence": _evidence(d.get("evidence"), turn_id)})
    own_mistakes = {r[0]: _norm(r[1]) for r in c.execute("select id, title from ledger where session_id=? and kind='mistake'", (session_id,))}
    for m in result.get("mistakes") or []:
        lesson = (m.get("lesson") or "").strip()
        title = str(m.get("title") or "")[:300]
        try:
            repeat = int(m.get("repeat_of")) if m.get("repeat_of") is not None else None
        except (TypeError, ValueError):
            repeat = None
        if repeat not in own_mistakes:
            repeat = None
            if m.get("repeat_of") is not None:  # flagged as a repeat but with a bad id: fall back to the same-titled mistake
                repeat = next((i for i, t in own_mistakes.items() if t == _norm(title)), None)
        if repeat:
            lesson = f"{lesson} (Repeated mistake — see #{repeat}.)".strip()
        add("mistake", {"title": title, "body": m.get("what_happened") or "",
                        "rationale": m.get("why_it_was_a_mistake"), "lesson": lesson or None, "status": "recorded",
                        "evidence": _evidence(m.get("evidence"), turn_id)})
    for x in result.get("corrections") or []:
        try:
            target = int(x.get("mistake_id"))
        except (TypeError, ValueError):
            continue
        if target not in own_mistakes or not ((x.get("lesson") or "").strip() or (x.get("cause") or "").strip()):
            continue
        store.create_correction(c, target, {"cause": x.get("cause"), "lesson": x.get("lesson"), "note": x.get("why"),
                                            "turn_id": turn_id, "evidence": _evidence(x.get("evidence"), turn_id)}, source="scribe", model=model)
        counts["correction"] += 1
    for f in result.get("failed_attempts") or []:  # older prompt shape
        add("mistake", {"title": str(f.get("attempt") or "")[:300], "body": f.get("why_it_failed") or "",
                        "what_worked": f.get("what_worked_instead"), "status": "recorded",
                        "evidence": _evidence(f.get("evidence"), turn_id)})
    for i in result.get("issues") or []:
        status = i.get("status") if i.get("status") in ("open", "resolved") else "open"
        add("issue", {"title": str(i.get("title") or "")[:300], "body": i.get("detail") or "", "status": status,
                      "evidence": _evidence(i.get("evidence"), turn_id)})
    for t in result.get("todos") or []:
        title = str(t.get("title") or "")[:300]
        if not title or _norm(title) in existing_todos:
            continue
        existing_todos.add(_norm(title))
        owner = t.get("owner") or "unknown"
        ev = ", ".join(str(x) for x in (t.get("evidence") or []))
        state = t.get("state") if t.get("state") in ("not_started", "in_progress", "waiting", "blocked") else "not_started"
        note = (t.get("note") or "").strip()
        store.create_todo(c, session_id, {"title": title, "priority": t.get("priority") if t.get("priority") in db.PRIORITIES else "P2",
                                          "state": state,
                                          "notes": f"Owner: **{owner}**" + (f"\n\nEvidence: turn \x60{turn_id[-8:]}\x60 items {ev}" if ev else "")
                                          + (f"\n\n- {time.strftime('%b %d %H:%M')} · {note}" if note else ""),
                                          "source_ref": turn_id}, created_by="extractor")
        counts["todo"] += 1
    for u in result.get("updates") or []:
        try:
            note = (u.get("note") or "").strip()
            if u.get("ledger_id"):
                e = store.get_ledger(c, int(u["ledger_id"]))
                if e["session_id"] != session_id:
                    continue
                fields = {}
                if u.get("status") in db.LEDGER_STATUS[e["kind"]] and e["status"] != u["status"]:
                    fields["status"] = u["status"]
                if note and e["kind"] in ("issue", "decision"):
                    fields["body"] = ((e["body"] or "").rstrip() + "\n\n" if e["body"] else "") + f"Update {time.strftime('%b %d %H:%M')}: {note}"
                if fields:
                    store.update_ledger(c, e["id"], fields, actor="extractor")
                    counts["update"] += 1
            elif u.get("todo_id"):
                t = store.get_todo(c, int(u["todo_id"]))
                if t["session_id"] != session_id:
                    continue
                changed = False
                if u.get("state") in db.TODO_STATES and t["state"] != u["state"]:
                    store.update_todo(c, t["id"], {"state": u["state"]}, actor="extractor")
                    changed = True
                if note and note not in (t["notes"] or ""):
                    _append_note(c, store.get_todo(c, t["id"]), note, turn_id, "extractor")
                    changed = True
                counts["update"] += changed
        except (KeyError, ValueError, TypeError):
            continue
    for d in result.get("drop") or []:
        try:
            t = store.get_todo(c, int(d.get("todo_id")))
        except (KeyError, ValueError, TypeError):
            continue
        if t["session_id"] == session_id:
            store.delete_todo(c, t["id"], actor="extractor", reason=d.get("reason"))
            counts["drop"] += 1
    folder = (c.execute("select artifacts_dir from sessions where id=?", (session_id,)).fetchone() or [None])[0]
    for a in result.get("artifacts") or []:
        path = os.path.abspath(os.path.expanduser(str(a.get("path") or "")))
        if not a.get("path") or (folder and path.startswith(os.path.abspath(folder) + os.sep)) or projects.protected(path):
            continue  # folder files are scanned; the service never touches macOS-protected folders
        try:
            artifacts.link_file(c, session_id, path, (a.get("title") or "").strip() or None)
            counts["artifact"] += 1
        except OSError:
            continue
    return counts


class ExtractorWorker(threading.Thread):
    def __init__(self, source=None, client=None):
        super().__init__(name="extractor", daemon=True)
        self.src = source or CodexSource()
        self.client = client or Copilot()
        self.stop_event = threading.Event()
        self.wake = threading.Event()

    def reset_stale(self, c):
        with db.write(c):
            c.execute("update turns set extract_status='pending' where extract_status='running'")

    def next_job(self, c):
        with db.write(c):
            row = c.execute("""select session_id, turn_id from turns where extract_status='pending'
                               order by completed_at limit 1""").fetchone()
            if row:
                c.execute("update turns set extract_status='running' where session_id=? and turn_id=?", (row[0], row[1]))
            return dict(row) if row else None

    def run(self):
        c = db.conn()
        self.reset_stale(c)
        while not self.stop_event.is_set():
            job = self.next_job(c)
            if not job:
                self.wake.wait(config.POLL_SECONDS)
                self.wake.clear()
                continue
            self.process(c, job["session_id"], job["turn_id"])

    def process(self, c, session_id, turn_id):
        try:
            items = self.src.items_for_turn(session_id, turn_id)
            totals = {}
            limit = self.client.max_prompt_tokens(config.EXTRACT_MODEL) if items else None
            for n, user in enumerate(build_prompts(lambda: context_block(c, session_id), turn_id, items, limit), 1):
                text, usage, secs = self.client.chat(config.EXTRACT_MODEL, SYSTEM, user, config.EXTRACT_EFFORT)
                result = parse_json(text)
                with db.write(c):
                    counts = apply_result(c, session_id, turn_id, result, config.EXTRACT_MODEL)
                    db.log_event(c, "extractor", "turn.extracted", session_id,
                                 {"turn_id": turn_id, "part": n, "secs": round(secs, 2), "usage": usage, "counts": counts,
                                  "model": config.EXTRACT_MODEL, "effort": config.EXTRACT_EFFORT, "prompt": PROMPT_VERSION})
                for k, v in counts.items():
                    totals[k] = totals.get(k, 0) + v
            with db.write(c):
                c.execute("update turns set extract_status='done', extract_error=null, extracted_at=? where session_id=? and turn_id=?",
                          (db.now_ms(), session_id, turn_id))
            return totals
        except Exception as e:  # recorded and visible; re-run from the UI or MCP
            log.warning("extraction failed for %s/%s: %s", session_id, turn_id, e)
            with db.write(c):
                c.execute("update turns set extract_status='failed', extract_error=? where session_id=? and turn_id=?",
                          (str(e)[:1000], session_id, turn_id))
                db.log_event(c, "extractor", "turn.failed", session_id, {"turn_id": turn_id, "error": str(e)[:1000]})
            return None


def build_prompts(context, turn_id, items, max_prompt_tokens=None, system=None, with_facts=True):
    """The user prompt(s) for one turn: existing ledger + computed facts + rendered activity, split if the turn is too long.
    context is a string or a callable returning one (re-read per chunk so later chunks see earlier chunks' entries).
    Shared by the worker and the evaluation harness so both send exactly the same input."""
    lines = delta.render(items)
    if not lines:
        return []
    facts = "\n".join(delta.facts(items)) if with_facts else ""
    ctx = context if callable(context) else (lambda: context)
    budget = None
    if max_prompt_tokens:
        # ~3 characters per token is conservative for transcripts full of code and JSON.
        budget = max(4000, max_prompt_tokens * 3 - len(system or SYSTEM) - len(ctx()) - len(facts) - 2000)
    chunks = delta.chunk(lines, budget) if budget else ["\n".join(lines)]
    for n, part in enumerate(chunks, 1):
        header = f"Turn {turn_id} (part {n} of {len(chunks)})" if len(chunks) > 1 else f"Turn {turn_id}"
        yield ctx() + (f"\n\n{facts}" if facts else "") + f"\n\n{header} activity:\n" + part


def recheck_mistake(c, client, source, ledger_id):
    """Ask the scribe to verify one mistake against its turn; append a correction if the cause or lesson is wrong or missing."""
    e = store.get_ledger(c, ledger_id)
    if e["kind"] != "mistake":
        raise ValueError("only mistakes can be re-checked")
    turn_id = e.get("turn_id") or next((x.get("turn_id") for x in e.get("evidence") or [] if isinstance(x, dict) and x.get("turn_id")), None)
    if not turn_id:
        raise ValueError("this mistake has no source turn to re-check against")
    items = source.items_for_turn(e["session_id"], turn_id)
    if not items:
        raise ValueError("the source turn is no longer in Codex's history")
    record = {"title": e["title"], "what_happened": e["body"], "cause": e.get("effective_cause"), "lesson": e.get("effective_lesson"),
              "corrections": [{k: x.get(k) for k in ("cause", "lesson", "note", "source")} for x in e.get("corrections") or []]}
    user = ("Recorded mistake:\n" + json.dumps(record, indent=1, ensure_ascii=False) + "\n\n" + "\n".join(delta.facts(items))
            + f"\n\nTurn {turn_id} activity:\n" + "\n".join(delta.render(items)))
    text, usage, secs = client.chat(config.EXTRACT_MODEL, RECHECK_SYSTEM, user, config.EXTRACT_EFFORT)
    out = parse_json(text)
    result = {"verdict": out.get("verdict") or "confirmed", "secs": round(secs, 1)}
    if result["verdict"] == "corrected" and ((out.get("lesson") or "").strip() or (out.get("cause") or "").strip()):
        with db.write(c):
            result["correction"] = store.create_correction(
                c, ledger_id, {"cause": out.get("cause"), "lesson": out.get("lesson"), "note": out.get("why"), "turn_id": turn_id,
                               "evidence": _evidence(out.get("evidence"), turn_id)}, source="scribe", model=config.EXTRACT_MODEL)
    else:
        result["verdict"] = "confirmed"
    with db.write(c):
        db.log_event(c, "extractor", "ledger.rechecked", e["session_id"], {"id": ledger_id, "verdict": result["verdict"], "secs": result["secs"]})
    return result


def queue_session(c, session_id, only_missing=True):
    """Queue a session's finished turns for extraction (on-demand backfill / re-run)."""
    cond = "extract_status in ('skipped','failed')" if only_missing else "extract_status!='running'"
    cur = c.execute(f"""update turns set extract_status='pending', extract_error=null
                        where session_id=? and status in ('completed','failed','interrupted') and {cond}""", (session_id,))
    return cur.rowcount

