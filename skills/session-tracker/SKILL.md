---
name: session-tracker
description: Use in every Codex session. The session tracker holds the session's durable state — todos, subagents with their task/status/result, the ledger (decisions, issues = triage log, mistakes with lessons) and artifacts — kept current by a scribe after every turn. Read it before answering status or progress questions, before debugging or retrying, when asked what was decided or when designing from earlier discussion, when asked about an earlier report or file, when continuing earlier work, and after compaction. Also covers when to write to it yourself and what to do when it is not running.
---

# Session tracker

The tracker is this session's memory outside the context window. It survives compaction, restarts and other sessions. Use it instead of what you remember: after compaction your memory of tasks, results and decisions is a lossy summary; the tracker is not.

## Who writes what

- **The service (no model), within seconds:** the session tree, each subagent's spawn task, status (new, running, stalled, done, failed, interrupted, archived) and, once it finishes, its result; files in the session's artifacts folder; Workflow Canvas documents you touch; failed turns as issues.
- **The scribe (after every turn):** todos for every task you or the user identify (with state, owner, notes, delegation, completion, deletion when dropped), decisions, issues, mistakes with lessons, and deliverables saved outside the artifacts folder. It lags the current turn by one.
- **You:** mostly read. Write only in the cases under "When to write".

Your calls are attributed to your session automatically; pass session_id only for another session.

## When to read (do this, don't answer from memory)

| Situation | Read |
|---|---|
| Start, resume, or after compaction | The injected [session-tracker] digest; if absent, get_session once |
| Asked for status, progress, what's done or left; before reporting on work | get_session with include ["todos", "children"]: todos and children (task, status, result). Reconcile with results that arrived this turn |
| An error, a failing check, debugging, or about to retry something | search with the error text, tool or command; read open issues and every mistake's lesson (effective_lesson if corrected) |
| Asked what was decided, why, or designing from earlier discussion | Decisions in get_session; skip superseded ones; search for older ones |
| Asked about an earlier report, document, plan, screenshot or file | Artifacts in get_session, or search; open the path it gives |
| Asked to continue or build on earlier work or another chat | search or get_tree to find that session, then get_session with its session_id |
| Delegating to subagents, or integrating a subagent's work | The child's node (get_session with its session_id) before you report or build on it |

The digest also lists open issues and lessons from earlier sessions on the same repository. Treat them like your own.

## When to write

Write only when it cannot wait for the scribe, or the user asks:
- **Todos:** add_todo for new work you are about to hand off or report on in this same turn; update_todo (state and a short note: who has it, result, blocker) when you report status before the turn ends; delete_todo when a task is dropped and you are reporting the board now. Check the existing list first; never add a duplicate.
- **Decisions:** record a decision when its exact wording matters (the user's call, an architecture choice); update_ledger to mark it decided or superseded.
- **Mistakes:** record your own mistake as soon as you notice it: what happened, root cause, and a lesson naming the check that would have prevented it. Mistakes are permanent.
- **Issues:** when debugging, say clearly in your message what you found (symptom, hypothesis or confirmed cause, repro, fix); the scribe records it. Use update_ledger to resolve an issue at once if someone relies on that now.
- **Artifacts:** save deliverables in the session's artifacts folder (artifacts_dir in get_session). The tracker never reads macOS-protected folders (~/Documents, ~/Desktop, ~/Downloads, which hold Codex chat folders), so copy a deliverable made there into the artifacts folder. add_artifact only for a file elsewhere that someone needs to find now.

## Orchestrating subagents

- One todo per task; note which subagent has it. The scribe does this after your turn; do it yourself only if you report the board in the same turn.
- A subagent's task, status and result are recorded from Codex automatically; read them instead of recalling them.
- When asked what is done: done = the todo is done or the subagent working on it finished with a result; running or stalled = in progress; interrupted = cancelled. Name anything you cannot account for.

## Tools (MCP server session_tracker)

get_session, get_tree, search, add_todo, update_todo, delete_todo, attach_to_todo, record (decision | mistake | issue), update_ledger, add_artifact, link_canvas, get_digest, extract_now.

## Rules

- The tracker never blocks the user's task; it is memory, not a gate.
- Do not call get_session every turn. Read on the triggers above.
- Never edit, delete or reword mistakes or delete ledger entries; only the user can, from the UI.
- Use only the session_tracker MCP tools. Never read or write the tracker's database, files or HTTP API directly, and never search its source code for workarounds.

## When the tracker is not available

Detect it once, tell the user once, and carry on with the task.

1. **No session_tracker tools in your tool list:** the tracker is not installed for this Codex. Tell the user: "The session tracker isn't connected. To install it, clone https://github.com/paguilar1227/session-tracker and run ./install.sh in it." Do not install it yourself unless asked.
2. **A tool returns "session-tracker service is not reachable":** first run curl -fsS http://127.0.0.1:8795/api/health. If it answers, the service is fine and only this session's connection to it is broken: tell the user once (restarting the Codex session reconnects it) and do not restart anything. If it does not answer (or the sandbox blocks the check), you may run the restart command the error message gives, then retry once; a restart is harmless because the tracker catches up from Codex's records. If that fails, tell the user to re-run the install.sh named in the error message. Do not loop on retries.
3. **A tool says it could not tell which session is calling:** pass session_id explicitly (from the digest, get_tree, or the thread id).
4. **A tool call is blocked because it needs approval:** the installer pre-approves the tracker's tools. Tell the user once to run ./install.sh in the session-tracker folder (it sets default_tools_approval_mode = "approve" for session_tracker), and carry on.

While it is unavailable, mention any new todos, decisions or problems in your final message so nothing is lost. The tracker catches up from Codex's records when it is back, and the scribe still processes turns that finished while it was down.
