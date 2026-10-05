"""Run the production scribe prompt on eval sets. Usage:
  python3 eval/run.py --out RESULTS_DIR [--effort high] [--prompt-file FILE] SET_DIR [SET_DIR ...]
Each set dir has manifest.json mapping case -> {thread_id, turn_id}; turns are read from Codex's history (read-only)."""
import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from common import EMPTY_CONTEXT, items_for, load_manifest, source, with_backoff

from tracker import config, extractor


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("sets", nargs="+")
    ap.add_argument("--out", required=True)
    ap.add_argument("--effort", default=config.EXTRACT_EFFORT)
    ap.add_argument("--model", default=config.EXTRACT_MODEL)
    ap.add_argument("--prompt-file", help="override the scribe system prompt (for tuning)")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--no-facts", action="store_true", help="omit the computed facts block (to reproduce scribe-v5)")
    a = ap.parse_args()
    system = Path(a.prompt_file).read_text() if a.prompt_file else extractor.SYSTEM
    client, src = extractor.Copilot(), source()
    limit = client.max_prompt_tokens(a.model)
    jobs = [(Path(s).name, name, case) for s in a.sets for name, case in load_manifest(s).items()]

    def run(job):
        set_name, name, case = job
        out = Path(a.out) / set_name
        out.mkdir(parents=True, exist_ok=True)
        merged, secs, parts = {}, 0.0, 0
        for user in extractor.build_prompts(EMPTY_CONTEXT, case["turn_id"], items_for(src, case), limit, system, with_facts=not a.no_facts):
            def call(user=user):
                text, usage, s = client.chat(a.model, system, user, a.effort)
                return extractor.parse_json(text), s
            result, s = with_backoff(call)
            secs += s
            parts += 1
            for k, v in result.items():
                if isinstance(v, list):
                    merged.setdefault(k, []).extend(v)
        rec = {"model": a.model, "effort": a.effort, "prompt_file": a.prompt_file, "secs": secs, "parts": parts, "result": merged}
        (out / f"{name}.json").write_text(json.dumps(rec, indent=1, ensure_ascii=False))
        return set_name, name, round(secs, 1), len(merged.get("mistakes") or [])

    with ThreadPoolExecutor(a.workers) as ex:
        for row in ex.map(run, jobs):
            print(*row, flush=True)


if __name__ == "__main__":
    main()
