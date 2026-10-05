"""Shared helpers for the scribe evaluation harness. Eval sets (labelled turns) live outside the repo."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tracker.codex_source import CodexSource  # noqa: E402

EMPTY_CONTEXT = "Existing ledger:\n(empty)\n\nOpen todos:\n(none)"


def load_manifest(set_dir):
    """{case_name: {thread_id, turn_id}} from <set>/manifest.json (flat, or nested under "cases")."""
    m = json.loads((Path(set_dir) / "manifest.json").read_text())
    m = m.get("cases", m)
    return {k: v for k, v in m.items() if isinstance(v, dict) and v.get("turn_id")}


def items_for(src, case):
    return src.items_for_turn(case["thread_id"], case["turn_id"])


def source():
    return CodexSource()


def with_backoff(fn, attempts=6):
    """Retry rate limits (HTTP 429) and transient errors with exponential backoff."""
    import time
    from tracker import extractor
    for i in range(attempts):
        try:
            return fn()
        except extractor.ExtractError as e:
            if i == attempts - 1:
                raise
            time.sleep(min(60, 4 * 2 ** i) if "429" in str(e) else 2)


def respond(client, model, system, user, effort):
    """Copilot Responses API (models such as gpt-6-astra are not served on /chat/completions)."""
    d = client._request("POST", "/responses", {"model": model, "reasoning": {"effort": effort},
                                               "input": [{"role": "system", "content": system}, {"role": "user", "content": user}]})
    return "".join(p.get("text", "") for o in d.get("output", []) for p in (o.get("content") or []) if isinstance(p, dict))
