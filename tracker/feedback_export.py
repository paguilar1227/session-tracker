"""Turn your 👎 / ➕ labels into an evaluation set for the scribe (see README.md, "Improving the scribe")."""
import json
from pathlib import Path

from . import db, delta, store
from .codex_source import CodexSource


def export(out_dir, c=None, source=None):
    c = c or db.conn()
    src = source or CodexSource()
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    cases, key = {}, ["# Answer key from your feedback (generated)",
                      "",
                      "Each case is a turn you labelled in the session-tracker UI. F<n>.M1 items are mistakes you reported the scribe missed "
                      "(score 1 if a recorded mistake names it with its real cause and a lesson that would prevent it). Entries you flagged as "
                      "wrong are traps: recording them again is a false positive (fp_a).", ""]
    by_turn = {}
    for f in reversed(store.list_feedback(c)):
        if f.get("turn_id"):
            by_turn.setdefault((f["session_id"], f["turn_id"]), []).append(f)
    for n, ((session_id, turn_id), labels) in enumerate(by_turn.items(), 1):
        items = src.items_for_turn(session_id, turn_id)
        if not items:
            continue
        name = f"F{n:02d}"
        (out / f"{name}.txt").write_text("\n".join(delta.render(items)))
        cases[name] = {"thread_id": session_id, "turn_id": turn_id, "feedback_ids": [f["id"] for f in labels]}
        key.append(f"## {name} — session {session_id[-12:]} turn {turn_id[-8:]}")
        m = 0
        for f in labels:
            if f["kind"] == "missed":
                m += 1
                key.append(f"- **{name}.M{m}** (missed mistake you reported): {f['note']}")
            else:
                key.append(f"- **Trap (fp_a):** the scribe's {f.get('ledger_kind') or 'entry'} \"{f.get('ledger_title')}\" was flagged wrong"
                           + (f": {f['note']}" if f.get("note") else "."))
        key.append("")
    (out / "manifest.json").write_text(json.dumps({"meta": {"source": "session-tracker feedback"}, "cases": cases}, indent=1))
    (out / "answer_key.md").write_text("\n".join(key) + "\n")
    return {"cases": len(cases), "dir": str(out)}
