#!/usr/bin/env python3
"""Collects real claims to benchmark SourceTrace: on each page, finds paragraphs that state a
share or number AND cite another site, and turns each into a claim like '47.2% of U.S. adults met'.

Usage: python3 bench/harvest.py bench/urls-dev.txt > bench/claims-dev.json
"""
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sourcetrace import Page, cited_links, fetch, host_of, is_year, norm, quantities  # noqa: E402

PER_PAGE = 4


def is_identifier(text, m):
    """Numbers that aren't statistics: ISBNs and standard numbers ('978-0-596', 'SP 800-207',
    '2018/851'), page ranges ('190–191'), listicle titles ('205 Cybersecurity Stats'), names
    ('404 Media'), episode numbers ('368: The Future of QA')."""
    before, after = text[max(0, m.start() - 1):m.start()], text[m.end():m.end() + 40]
    if re.match(r"[-/–:]\s*\d", after) or re.match(r"[-/–]", before):
        return True
    if not (m["pct"] or m["n"] or m["frac"]) and re.match(r"[:\s]*[A-Z]", after):  # '404 Media', '368: The'
        return True
    return re.search(r"\b(statistics|stats|facts|trends)\b", after[:30], re.I) is not None


def claims_from(url):
    try:
        kind, body, final = fetch(url)
    except Exception as e:
        print(f"skip {url}: {e}", file=sys.stderr)
        return []
    if kind != "html":
        return []
    page = Page()
    page.feed(body)
    out, seen = [], set()
    for i, (text, _) in enumerate(page.blocks):
        text = norm(text)
        if len(text) < 60:
            continue
        external = [u for u in cited_links(page, [i], final) if host_of(u) != host_of(final)]
        if not external:
            continue
        for m, value, share, _ in quantities(text):
            if is_year(m) or (not share and value < 100):  # years and small counts make poor claims
                continue
            if is_identifier(text, m):
                continue
            words = text[m.start():].split()[:8]
            claim = " ".join(words).rstrip(".,;:)")
            if value in seen or len(words) < 4:
                continue
            seen.add(value)
            out.append({"claim": claim, "url": url, "sentence": text[max(0, m.start() - 150):m.end() + 150]})
            break
        if len(out) >= PER_PAGE:
            break
    print(f"{len(out)} claims from {url}", file=sys.stderr)
    return out


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    urls = [u.strip() for u in Path(sys.argv[1]).read_text().splitlines() if u.strip() and not u.startswith("#")]
    with ThreadPoolExecutor(8) as pool:
        claims = [c for batch in pool.map(claims_from, urls) for c in batch]
    json.dump(claims, sys.stdout, indent=1, ensure_ascii=False)


if __name__ == "__main__":
    main()
