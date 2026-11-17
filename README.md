# SourceTrace

**Follow a statistic back to where it actually came from.**

A free, open-source fact-checking tool for statistics: give it a number and the page where you saw it, and it traces the citation chain to the original source. Works from the command line, in Docker, or as an AI agent skill for Claude Code and other agents.

[![tests](https://github.com/BurnMeDown-AI/SourceTrace/actions/workflows/tests.yml/badge.svg)](https://github.com/BurnMeDown-AI/SourceTrace/actions/workflows/tests.yml)

Numbers get copied from article to article until nobody can say where they started. On the way they change: "4%" becomes "40%", "large projects" quietly drops off, a 2009 figure gets cited as current. AI-written articles make this worse, and AI assistants repeat whatever they read.

SourceTrace takes a claim and the page where you saw it. It follows that page's citations hop by hop, checks whether each source really states that number about that topic, and tells you where the trail ends.

```
$ sourcetrace "a quarter of incoming pull requests" https://www.theregister.com/.../5304389

✔ https://www.theregister.com/...
    "Maintainers reckon a quarter of incoming pull requests are slop, rising to 31% on larger projects…"
    ✔ https://www.linuxfoundation.org/press/new-lf-research-survey-...
        "…but 25% of Incoming Pull Requests are Considered AI Slop…"
        ✔ https://www.linuxfoundation.org/research/oss-development-gen-ai-era-2026
            "The findings reveal that 1 in 4 incoming pull requests are low-quality "AI slop"…"

VERDICT: TRACED
Found the earliest page(s) that state the number and cite nothing further for it:
  https://www.linuxfoundation.org/research/oss-development-gen-ai-era-2026  (publication date unknown)
```

"A quarter", "25%" and "1 in 4" are the same number, written three ways on three sites. SourceTrace followed it from a news story, to a press release, to the research report.

## Verdicts

| Verdict | Meaning |
|---|---|
| **TRACED** | Found the earliest page(s) that state the number and cite nothing further for it, with the date each was published. Cite that page. If it's more than 5 years old, the output says to look for a newer figure. |
| **DIFFERENT NUMBER** | A cited source talks about the same thing but gives a different number. The tool quotes the passage so you can compare. |
| **UNVERIFIED** | The cited sources opened, but none states the number in its readable text. It may be in a chart, an image of a table or a paywalled paper, or the citation may be wrong. |
| **ORPHAN** | The page states the number but cites nothing for it. |
| **UNREACHABLE** | The starting page, or every cited source, couldn't be opened, live or archived. |
| **CHECK PDF** | The trail ends at a PDF that couldn't be read (no poppler installed, or a scan). |
| **INCOMPLETE** | SourceTrace stopped before the trail ended (`--depth`, `--pages` or `--timeout`). Rerun with a higher limit. |
| **NOT FOUND** | The starting page doesn't state this number about this topic. |

## How well it works

Every result below was checked by hand. Each claim is a real sentence from a real page: a number plus the words around it, with a citation to another site in the same paragraph.

### On pages never seen while building it

**73 claims from 19 pages** of health and workplace statistics round-ups, news articles and Wikipedia, collected after v0.5's features were written:

| Verdict | Claims | Checked by hand |
|---|---|---|
| TRACED | 32 | One "claim" was a year. Of the other 31, **24 (77%) led to a page that really states that number about that topic.** These include BLS and UK HSE report PDFs, WHO and CDC pages, and the 2021 Mind Share Partners study behind the "84% of employees" figure that round-ups repeat. |
| UNVERIFIED | 29 | The cited page doesn't contain the figure in readable text. |
| DIFFERENT NUMBER | 6 | 3–4 were real. Best catch: a page says 58% of U.S. gun deaths were suicides; its source says 62%. |
| other | 6 | PDFs to check by hand, pages that cite nothing, start pages that don't have the number. |

The 7 wrong TRACED results fell into three patterns: a *change* matched as a *share* ("a 40% increase" for "40% of employees"), statistical noise ("95% confidence interval", the edge of an error range), and the wrong group ("half of men at their level"). **After seeing them, the first two patterns were fixed.** With v0.5.1, 29 of 32 (91%) are correct, but that number is no longer from unseen data. The 3 still wrong state the same number for a different measure or group, which is what the AI agent's meaning check is for.

### On an earlier held-out set

**99 claims from 26 pages** (cybersecurity and consumer-survey round-ups, Wikipedia). v0.4 was measured on these before any of its fixes were tuned on them: **26 of 29 real TRACED results were correct (90%)**. With v0.5, 25 of 28 are correct; with v0.5.1, **25 of 27 (93%)**.
- v0.5 removed one wrong answer: "2 in 10 Democrats" for "2 in 10 adults".
- It traced the four "$4.99 million" claims to IBM's report for the right reason, now that it reads "4.99M".
- v0.5.1 no longer follows another site's PDF from a landing page, which had matched "41.9% obese" to "high attrition (41.9%)".

### What that means

- **Most numbers on statistics round-up pages can't be confirmed from the page they cite.** That is the problem SourceTrace exists to show.
- **When it says TRACED, it's right about 8 or 9 times in 10.** Check the quoted passage before you rely on it. The AI agent skill tells agents to do exactly that.
- **Speed:** a median of 3–15 seconds per claim. 90% finish within 50 seconds. The slow ones wait on the Wayback Machine, or read a full report as well as its landing page.

## Install

Needs Python 3.9 or newer. There are no Python dependencies.

```bash
pip install git+https://github.com/BurnMeDown-AI/SourceTrace
sourcetrace "47% of adults met the guidelines" https://example.com/article
```

Or without installing: clone the repo and run `python3 sourcetrace.py ...`.

For PDF sources, install poppler (`brew install poppler` or `apt install poppler-utils`). For scanned PDFs, also install tesseract (`brew install tesseract` or `apt install tesseract-ocr`).

### Or use Docker, with everything included

```bash
docker build -t sourcetrace https://github.com/BurnMeDown-AI/SourceTrace.git
docker run --rm sourcetrace "47% of adults met the guidelines" https://example.com/article
```

The image includes poppler, tesseract and Python 3.14.

## Usage

```
sourcetrace CLAIM URL [--number N] [--depth 3] [--links 5] [--pages 30] [--timeout 60] [--json]
```

- **CLAIM**: the number plus a few words of what it measures, like `"47% of adults met the guidelines"`. The words matter: they're how SourceTrace tells this 47% apart from any other 47% on a page.
- **--number**: if the claim has several numbers (`"$4.99 million, up 12%"`), which one to trace. By default, the first.
- **--depth**: how many citation hops to follow.
- **--links**: how many citations to follow per page.
- **--pages**: the most pages to fetch in total.
- **--timeout**: seconds before it stops opening new pages. Whatever it hasn't reached is reported, not dropped.
- **--json**: machine-readable output, with the verdict, the source URLs, each origin's date and quoted passage, and the full trace.
- **Exit code**: 0 when the verdict is TRACED, 1 otherwise, so you can use it in scripts and CI.

## What it understands

**Numbers, however they're written.**
- "25%" matches "25 percent", "a quarter of", "one in four" and "twenty-five percent".
- "368 million" matches "368,000,000"; "$4.99 million" matches "$4.99M".
- European formats work: "25,5 %", "1 967", "1.967.000", and The Lancet's "1·27".
- "25.5%", "125%", "5G" and the year 2025 don't match 25%.

**What the number measures.**
- A common number like "20%" only counts if two words from the claim are in its own sentence (or the one before, for "Two-thirds of them…").
- It tells shares from changes: "40% of employees" ≠ "a 40% increase".
- It tells groups apart: "2 in 10 adults" ≠ "2 in 10 Democrats".
- It ignores statistical noise: "95% confidence interval", the edges of an error range.
- Very specific numbers (21.7%, 30,903) count anywhere.
- DIFFERENT NUMBER needs the same kind of number about the same group, and ignores the source's reference list (a cited book titled "How America throws away nearly half of its food" isn't a finding).

**Citations.**
- It follows inline links, Wikipedia footnotes (only the footnote on the number's own sentence) and numbered references like `[12]`.
- It also checks citations a paragraph or two away from the number.
- Share buttons, menus, homepages, contact and sign-up pages, and library catalogs are ignored.

**Where sources hide their numbers.**
- **Landing pages:** if a cited page doesn't state the number, it reads that page's "Download the full report" link, or a PDF on the same site.
- **Repositories:** DSpace sites (FAO, WHO IRIS, many universities) show their files through a JavaScript viewer; it reads the file itself.
- **Paywalled papers:** it finds a free legal copy (arXiv, PubMed Central, the author's site) through [OpenAlex](https://openalex.org).
- **PDFs:** read through poppler, including links that wrap across lines.
- **Scanned PDFs:** read with tesseract OCR.
- **Dead and blocked pages:** it reads the newest Wayback Machine copy. That includes "are you a robot?" pages sent in place of the real one.
- **Pages built with JavaScript:** it reads the article from the page's schema.org or Next.js data.

**Dates.** Each origin shows when it was published, from the page's metadata, its URL or the PDF's info. If the number is more than 5 years old, the output says to look for a newer figure.

## Use it from an AI agent

AI assistants repeat statistics they can't trace. [`skills/sourcetrace/SKILL.md`](skills/sourcetrace/SKILL.md) is an [Agent Skill](https://github.com/anthropics/skills) that tells an agent to:
- **Trace a number before citing it**, and act on each verdict.
- **Check the meaning itself.** SourceTrace matches numbers and words; the agent reads each origin's quoted passage and confirms it's the same group, measure and period as the claim.
- **Search first when there's no URL.** Given just a statistic, the agent searches the web for it and traces the best results.

To use it with Claude Code, copy the `skills/sourcetrace` folder into `~/.claude/skills/`. Other agents can follow the same file.

## Limits

- **TRACED is where the trail ends, not proof the number is true.** It doesn't judge whether the original study was sound.
- **On its own, it matches numbers and words, not meaning.** Used alone, it can still confuse groups or topics that share the same words, like "half of senior women" vs "half of men at their level". Used through an AI agent, the agent checks the meaning.
- **Charts and images.** Numbers only shown in a chart aren't seen. OCR covers scanned PDFs, not charts.
- **Paywalls and bot walls.** Some sites block automated requests, and not every paper has a free copy. It won't disguise itself to get around a block.

## Safety and privacy

- **It only opens public web pages.** Links to `localhost`, private network addresses or cloud metadata services, and anything that isn't `http(s)://`, are refused, including through redirects. A page can't use SourceTrace to reach inside your network.
- **Hostile pages can't take it down.** Downloads stop at 20 MB, decompressed size included. OCR is limited in pages, image size and time. Malformed pages and PDFs are reported, not crashed on.
- **Output is cleaned.** Terminal control codes and text-direction tricks in page text are removed before printing.
- **What leaves your machine:** besides the pages it reads, SourceTrace sends the address of a source it can't open to the Wayback Machine (archive.org), and a paper's DOI to OpenAlex, to find copies. It never sends your claim.
- **For AI agents:** quoted page text is untrusted. The skill tells agents to read it as evidence, never as instructions.

## Benchmark yourself

The `bench/` folder holds the scripts, page lists, claims and results behind the numbers above. There are three sets: `dev` (used while building), `holdout` and `fresh`. Each `results-*` file is one graded run, named by version (`results-fresh-unseen.json` is v0.5's first look at the fresh set).

```bash
python3 bench/harvest.py bench/urls-fresh.txt > claims.json   # pull real claims from real pages
python3 bench/run.py claims.json results.json                  # trace them all and summarise
python3 bench/review.py results.json TRACED                    # print each result for checking by hand
```

## Development

```bash
python3 -m unittest
```

## License

MIT
