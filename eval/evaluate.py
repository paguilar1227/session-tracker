"""One tuning iteration: run the scribe on every eval set, grade each with judge.py, print a summary.
Usage: python3 eval/evaluate.py --sets SETS.json --out RESULTS_DIR [--effort high] [--prompt-file FILE]
SETS.json: {"<set dir>": {"key": "<key.md>", "extra_key": "<optional generic key>"}, ...}"""
import argparse
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sets", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--effort", default="high")
    ap.add_argument("--prompt-file")
    ap.add_argument("--skip-run", action="store_true", help="only re-grade existing outputs")
    ap.add_argument("--no-facts", action="store_true")
    a = ap.parse_args()
    sets = json.loads(Path(a.sets).read_text())
    out = Path(a.out)
    if not a.skip_run:
        cmd = [sys.executable, str(HERE / "run.py"), "--out", str(out), "--effort", a.effort, *sets]
        if a.prompt_file:
            cmd += ["--prompt-file", a.prompt_file]
        if a.no_facts:
            cmd.append("--no-facts")
        subprocess.run(cmd, check=True)

    def grade(item):
        set_dir, spec = item
        name = Path(set_dir).name
        cmd = [sys.executable, str(HERE / "judge.py"), "--key", spec["key"], "--set", set_dir, "--outputs", str(out / name),
               "--out", str(out / f"scores-{name}.json")]
        if spec.get("extra_key"):
            cmd += ["--extra-key", spec["extra_key"]]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode:
            raise SystemExit(f"judge failed for {name}: {r.stderr[-800:]}")
        return name, json.loads(r.stdout.strip().splitlines()[-1])

    with ThreadPoolExecutor(2) as ex:
        results = dict(ex.map(grade, sets.items()))
    total = {"M": [0, 0], "FC": [0, 0], "K": [0, 0], "fp": 0, "key_gap": 0}
    for name, s in results.items():
        print(f"{name:16} M {s['M'][0]:>4}/{s['M'][1]:<3} FC {s['FC'][0]:>4}/{s['FC'][1]:<3} K {s['K'][0]:>5}/{s['K'][1]:<3} FP {s['fp']} gaps {s['key_gap']}")
        for k in ("M", "FC", "K"):
            total[k][0] += s[k][0]
            total[k][1] += s[k][1]
        total["fp"] += s["fp"]
        total["key_gap"] += s["key_gap"]
    print(f"{'TOTAL':16} M {total['M'][0]:>4}/{total['M'][1]:<3} FC {total['FC'][0]:>4}/{total['FC'][1]:<3} K {total['K'][0]:>5}/{total['K'][1]:<3} FP {total['fp']} gaps {total['key_gap']}")
    (out / "summary.json").write_text(json.dumps({"sets": results, "total": total}, indent=1))


if __name__ == "__main__":
    main()
