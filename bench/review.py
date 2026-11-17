#!/usr/bin/env python3
"""Prints each benchmark result compactly for grading by hand: the claim, the verdict, and for
each source, what SourceTrace saw there.

Usage: python3 bench/review.py bench/results.json [VERDICT]
"""
import json
import sys

if len(sys.argv) < 2:
    sys.exit(__doc__)
results = json.load(open(sys.argv[1]))
only = sys.argv[2] if len(sys.argv) > 2 else None


def walk(n, depth=0):
    mark = {"found": "OK", "different": "DIFF"}.get(n["status"], n["status"][:4].upper())
    detail = n["context"] or n["note"]
    print(f"{'   ' * depth}  [{mark}] {n['url'][:95]}\n{'   ' * depth}        {detail[:200]}")
    for c in n["children"]:
        walk(c, depth + 1)


for k, r in enumerate(results):
    if only and r["verdict"] != only:
        continue
    print(f"\n#{k} {r['verdict']}  claim: {r['claim']!r}")
    if r["tree"]:
        walk(r["tree"])
