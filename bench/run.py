#!/usr/bin/env python3
"""Runs SourceTrace on every claim in a claims file and summarises the verdicts.

Usage: python3 bench/run.py bench/claims-dev.json bench/results.json
"""
import json
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sourcetrace import Quantity, all_nodes, fetch, keywords, trace, verdict  # noqa: E402


# 120s, not the CLI's 60: here 12 claims run at once and share the archive's 2 request slots
def run_one(c):
    start = time.time()
    try:
        budget = [30, time.monotonic() + 120]
        root = trace(c["url"], Quantity(c["claim"]), keywords(c["claim"]), fetch, 3, 5, budget=budget)
        label, why, _ = verdict(root)
    except Exception as e:  # a crash is a bug worth seeing, not a reason to stop the run
        root, label, why = None, "CRASH", repr(e)
    return {**c, "verdict": label, "why": why, "tree": root, "seconds": round(time.time() - start, 1)}


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    claims = json.loads(Path(sys.argv[1]).read_text())
    t0 = time.time()
    with ThreadPoolExecutor(12) as pool:
        results = list(pool.map(run_one, claims))
    Path(sys.argv[2]).write_text(json.dumps(results, indent=1, ensure_ascii=False))
    nodes = [n for r in results if r["tree"] for n in all_nodes(r["tree"])]
    print(f"{len(results)} claims in {time.time() - t0:.0f}s, {len(nodes)} pages visited")
    for label, n in Counter(r["verdict"] for r in results).most_common():
        print(f"  {label:13} {n:4}  {n / len(results):.0%}")
    errors = Counter(n["note"].split("(")[-1][:40] for n in nodes if n["note"].startswith("could not"))
    print("page errors:", dict(errors.most_common(8)))
    print("pages read from archive:", sum(bool(n.get("archived")) for n in nodes))
    secs = sorted(r["seconds"] for r in results)
    print(f"seconds per claim: median {secs[len(secs) // 2]}, 90th percentile {secs[len(secs) * 9 // 10]}, "
          f"slowest {secs[-1]}")


if __name__ == "__main__":
    main()
