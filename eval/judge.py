"""Automatic grader for scribe outputs against a written answer key (for fast tuning iterations; final verdicts use a blind human-style judge).
Usage: python3 eval/judge.py --key KEY.md --set SET_DIR --outputs RESULTS_DIR/<set> --out SCORES.json
The key is Markdown with one '## <case>' section per case and key item ids like R1.M1, U06.FC1, T1.K1."""
import argparse
import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from common import load_manifest, respond, with_backoff

from tracker import extractor

JUDGE_MODEL, JUDGE_EFFORT = "gpt-6-astra", "high"
SYSTEM = """You grade the output of a 'scribe' model that records a coding agent's decisions, mistakes, issues and todos for ONE turn. You get the answer key's scoring rules, the key section for this case, the case text the scribe saw, and the scribe's JSON output. Apply the key exactly as written; be strict and consistent.

For every key item id listed for this case, return a score of 1, 0.5 or 0 following the rubric (for mistake items also sub-scores detected / root_cause / lesson). Items of type M and FC are scored ONLY from the scribe's "mistakes" list (and "corrections" if present); an item that appears only as a decision, issue or todo scores 0. Items of type K are scored from whichever list the key says.
Then classify EVERY recorded mistake that does not match a key item: "neutral" (listed as neutral in the key, or real and supported by the case text but small), "key_gap" (real, supported by the case text and consequential, but missing from the key), or a false positive "fp_a" (a listed trap), "fp_b" (unsupported or invented, or blames the agent for something the case attributes elsewhere), "fp_c" (trivial noise).
Return JSON only: {"items": {"<id>": {"score": n, "detected": n, "root_cause": n, "lesson": n, "note": str}}, "extra_mistakes": [{"title": str, "verdict": str, "why": str}]}"""


def split_key(text):
    """(preamble rules, {case_prefix: section text})."""
    parts = re.split(r"(?m)^## ", text)
    pre, cases = parts[0], {}
    for p in parts[1:]:
        head = p.splitlines()[0]
        name = re.split(r"[ —:]", head.strip(), 1)[0]
        if re.match(r"^[A-Z]\d", name):
            cases[name] = "## " + p
        else:
            pre += "\n## " + p
    return pre, cases


def item_ids(case, section):
    ids = sorted(set(re.findall(r"\b([A-Z]\d+\.(?:M|FC|K)\d+)\b", section)))
    return [i for i in ids if i.split(".")[0] == case.split("_")[0]]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", required=True)
    ap.add_argument("--set", required=True)
    ap.add_argument("--outputs", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--extra-key", help="extra key text appended to every case (e.g. a generic pre-registered key)")
    a = ap.parse_args()
    pre, sections = split_key(Path(a.key).read_text())
    client = extractor.Copilot()

    def grade(name):
        prefix = name.split("_")[0]
        section = sections.get(name) or sections.get(prefix) or next((v for k, v in sections.items() if k.startswith(prefix)), None)
        if section is None and a.extra_key:
            section = Path(a.extra_key).read_text().replace("H*.M1", prefix + ".M1")
        if section is None:
            return name, {"error": "no key section"}
        ids = item_ids(name, section)
        case_text = (Path(a.set) / f"{name}.txt").read_text()
        output = json.loads((Path(a.outputs) / f"{name}.json").read_text())["result"]
        user = (f"SCORING RULES:\n{pre}\n\nKEY FOR THIS CASE (item ids: {', '.join(ids) or 'none'}):\n{section}\n\n"
                f"CASE TEXT:\n{case_text}\n\nSCRIBE OUTPUT:\n{json.dumps(output, indent=1, ensure_ascii=False)}")
        g = with_backoff(lambda: extractor.parse_json(respond(client, JUDGE_MODEL, SYSTEM, user, JUDGE_EFFORT)))
        g["ids"] = ids
        return name, g

    names = sorted(load_manifest(a.set))
    with ThreadPoolExecutor(4) as ex:
        scores = dict(ex.map(grade, names))
    Path(a.out).write_text(json.dumps(scores, indent=1, ensure_ascii=False))
    print(json.dumps(summarize(scores)))


def summarize(scores):
    tot = {"M": [0, 0], "FC": [0, 0], "K": [0, 0], "fp": 0, "key_gap": 0, "neutral": 0}
    for g in scores.values():
        for i in g.get("ids", []):
            kind = re.search(r"\.(M|FC|K)\d", i).group(1)
            tot[kind][1] += 1
            tot[kind][0] += float((g.get("items", {}).get(i) or {}).get("score") or 0)
        for x in g.get("extra_mistakes") or []:
            v = x.get("verdict", "")
            if v.startswith("fp"):
                tot["fp"] += 1
            elif v in ("key_gap", "neutral"):
                tot[v] += 1
    return tot


if __name__ == "__main__":
    main()
