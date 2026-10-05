"""Turn items -> compact text for the extractor (same trimming as the model pilot)."""
import json
import re
from pathlib import Path

SECRET = re.compile(r"(-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)"
                    r"|sk-[A-Za-z0-9_\-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|glpat-[A-Za-z0-9_\-]{20,}|xox[abp]-[A-Za-z0-9\-]{10,}"
                    r"|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_\-]{30,}|xai-[A-Za-z0-9]{20,}|(?i:bearer)\s+[A-Za-z0-9._~+/\-]{16,}=*"
                    r"|eyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"
                    r"|(?i:(?:api[_-]?key|token|secret|password|key)\s*[=:]\s*)\S{8,}"
                    r"|[?&](?:key|token|access_token|api_key)=[A-Za-z0-9._\-]{8,})")
SKIP = {"reasoning", "sleep", "imageView", "subAgentActivity"}


def redact(s):
    return SECRET.sub("[REDACTED]", s or "")


def _tail(s, n):
    s = s or ""
    return s if len(s) <= n else "…" + s[-n:]


def _head(s, n):
    s = s or ""
    return s if len(s) <= n else s[:n] + "…"


def short_id(item_id):
    return (item_id or "")[-8:]


def render_item(item_id, typ, js):
    j = json.loads(js) if isinstance(js, str) else js
    sid = short_id(item_id)
    if typ in SKIP:
        return None
    if typ == "userMessage":
        txt = " ".join(c.get("text", "") for c in j.get("content", []) if c.get("type") == "text")
        return f"[{sid}] USER: {_head(txt, 6000)}"
    if typ == "agentMessage":
        return f"[{sid}] AGENT: {j.get('text', '')}"
    if typ == "commandExecution":
        code = j.get("exitCode")
        keep = 1500 if code not in (0, None) else 400
        return (f"[{sid}] COMMAND exit={code} status={j.get('status')}: {_head(j.get('command', ''), 800)}\n"
                f"  output: {_tail(j.get('aggregatedOutput') or '', keep)}")
    if typ == "fileChange":
        parts = []
        for ch in j.get("changes") or []:
            kind = ch.get("kind")
            kind = kind.get("type") if isinstance(kind, dict) else kind
            parts.append(f"{kind} {ch.get('path')}")
        return f"[{sid}] FILE_CHANGE status={j.get('status')}: {', '.join(parts)}"
    if typ in ("mcpToolCall", "dynamicToolCall"):
        err = j.get("error")
        res = j.get("result")
        return (f"[{sid}] TOOL {j.get('server', '')}.{j.get('tool', '')} status={j.get('status')} "
                f"args={_head(json.dumps(j.get('arguments')), 500)} "
                f"error={_head(json.dumps(err), 600) if err else None} "
                f"result={_head(json.dumps(res), 500) if res and not err else ''}")
    if typ == "collabAgentToolCall":
        keep = {k: v for k, v in j.items() if k in ("prompt", "receiverThreadIds", "agentsStates")}
        return f"[{sid}] SUBAGENT_CALL {j.get('tool')} status={j.get('status')}: {_head(json.dumps(keep), 800)}"
    if typ == "contextCompaction":
        return f"[{sid}] CONTEXT_COMPACTION"
    return f"[{sid}] {typ}: {_head(json.dumps(j), 300)}"


def render(items):
    """items: dicts with item_id, item_type, item_json. Returns list of rendered lines."""
    out = []
    for it in items:
        line = render_item(it["item_id"], it["item_type"], it["item_json"])
        if line:
            out.append(redact(line))
    return out


CHECK_CMD = re.compile(r"\b(pytest|unittest|tox|nox|npm (?:run )?(?:test|lint|build|check)|pnpm (?:run )?(?:test|lint|build)|yarn (?:test|lint|build)"
                       r"|vitest|jest|playwright|go (?:test|vet|build)|cargo (?:test|check|build|clippy)|swift (?:test|build)|xcodebuild"
                       r"|mvn (?:test|verify)|gradle\w* test|tsc\b|eslint|ruff|mypy|pyright|shellcheck|make (?:test|check)|docker build"
                       r"|(?:^|[;&|(]\s*|\b(?:python3?|node|bash|sh|zsh|uv run|npx|deno run|bun)\s+(?:-\S+\s+)*)[\w./~-]*test[\w.-]*\.(?:sh|py|mjs|js|ts)\b)", re.I | re.M)
PROBE_CMD = re.compile(r"\b(curl|wget|http|httpie|nc|ping)\b")


def _short_path(p):
    p = str(p or "")
    home = str(Path.home())
    return ("~" + p[len(home):] if p.startswith(home) else p)[:200]


def _cmd_line(cmd, pattern, n=160):
    """The line of a (possibly multi-line) command that matched, without the shell wrapper."""
    cmd = re.sub(r"^/bin/(?:zsh|bash) -lc ", "", cmd or "").strip().strip("'\"")
    lines = cmd.splitlines() or [""]
    hit = next((ln for ln in lines if pattern.search(ln)), lines[0])
    return _head(hit.strip(), n)


def facts(items):
    """What the agent demonstrably did in the turn, computed from Codex's records (not from what the agent says).
    Explicit zeros matter: they let the scribe spot claims of verification that never happened."""
    reads, searches, changed, checks, probes, failed, tools, spawned, waited = [], 0, [], [], [], 0, {}, [], {}
    commands = unparsed = 0

    def add(lst, v):
        if v and v not in lst:
            lst.append(v)

    for it in items:
        typ = it["item_type"]
        try:
            j = json.loads(it["item_json"]) if isinstance(it["item_json"], str) else it["item_json"]
        except ValueError:
            continue
        if typ == "commandExecution":
            commands += 1
            code = j.get("exitCode")
            if code not in (0, None):
                failed += 1
            actions = j.get("commandActions") or []
            if not actions or any(a.get("type") == "unknown" for a in actions):
                unparsed += 1
            for a in actions:
                if a.get("type") == "read":
                    add(reads, _short_path(a.get("path") or a.get("name")))
                elif a.get("type") in ("search", "listFiles"):
                    searches += 1
            cmd = j.get("command") or ""
            if CHECK_CMD.search(cmd):
                checks.append(f"{_cmd_line(cmd, CHECK_CMD)} → exit {code}")
            elif PROBE_CMD.search(cmd):
                probes.append(f"{_cmd_line(cmd, PROBE_CMD)} → exit {code}")
        elif typ == "imageView":
            add(reads, _short_path(j.get("path")))
        elif typ == "fileChange":
            for ch in j.get("changes") or []:
                kind = ch.get("kind")
                kind = kind.get("type") if isinstance(kind, dict) else kind
                add(changed, f"{_short_path(ch.get('path'))} ({kind})")
        elif typ in ("mcpToolCall", "dynamicToolCall"):
            name = f"{j.get('server') or j.get('namespace') or ''}.{j.get('tool') or ''}".strip(".")
            ok, bad = tools.get(name, (0, 0))
            tools[name] = (ok + (j.get("status") == "completed" and not j.get("error")), bad + bool(j.get("error") or j.get("status") == "failed"))
        elif typ == "collabAgentToolCall":
            if j.get("tool") in ("spawnAgent", "spawn_agent"):
                for t in j.get("receiverThreadIds") or []:
                    add(spawned, t[-12:])
            for t, st in (j.get("agentsStates") or {}).items():
                waited[t[-12:]] = (st or {}).get("status")
    out = ["What the agent demonstrably did in this turn (computed from Codex's records, not from the agent's words):"]
    out.append(f"- Files opened with plain read commands ({len(reads)}): " + (", ".join(reads) if reads else "none")
               + (f" — plus {unparsed} scripts/pipelines whose reads are not itemized" if unparsed else ""))
    out.append(f"- Searches / file listings: {searches}")
    out.append(f"- Files changed ({len(changed)}): " + (", ".join(changed) if changed else "none"))
    out.append(f"- Commands run: {commands} ({failed} exited non-zero)")
    out.append(f"- Tests, builds, linters or type checks run ({len(checks)}): " + ("; ".join(checks) if checks else "none"))
    if probes:
        out.append(f"- HTTP / network probes ({len(probes)}): " + "; ".join(probes))
    if tools:
        out.append("- Tool calls: " + ", ".join(f"{k} ×{a + b}" + (f" ({b} failed)" if b else "") for k, (a, b) in tools.items()))
    if spawned or waited:
        out.append(f"- Subagents spawned: {', '.join(spawned) or 'none'}; last known states: "
                   + (", ".join(f"{k} {v}" for k, v in waited.items()) or "none"))
    return [redact(line) for line in out]


def chunk(lines, max_chars):
    """Split rendered lines into chunks no longer than max_chars (a single oversized line is truncated)."""
    chunks, cur, size = [], [], 0
    for line in lines:
        if len(line) > max_chars:
            line = line[:max_chars - 20] + " …[truncated]"
        if cur and size + len(line) + 1 > max_chars:
            chunks.append("\n".join(cur))
            cur, size = [], 0
        cur.append(line)
        size += len(line) + 1
    if cur:
        chunks.append("\n".join(cur))
    return chunks

