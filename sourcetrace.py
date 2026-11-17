#!/usr/bin/env python3
"""SourceTrace: follow a statistic back through its citations to where it started.

Usage: python3 sourcetrace.py "25% of pull requests are AI slop" https://example.com/article
"""
import argparse
import ipaddress
import json
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import zlib
from html.parser import HTMLParser
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.parse import quote, unquote, urldefrag, urljoin, urlparse
from urllib.request import HTTPCookieProcessor, HTTPRedirectHandler, Request, build_opener

__version__ = "0.5.1"
UA = f"Mozilla/5.0 (compatible; SourceTrace/{__version__}; +https://github.com/BurnMeDown-AI/SourceTrace)"
ARCHIVE = "https://web.archive.org/web/2099id_/"  # a far-future date returns the newest snapshot
AVAILABLE = "https://archive.org/wayback/available?url="  # fast 'is there a copy?' lookup
ARCHIVE_SLOTS = threading.Semaphore(2)  # the Wayback Machine refuses connections if we're greedy
BLOCK_TAGS = {"p", "li", "td", "th", "div", "blockquote", "figcaption", "dd", "dt",
              "h1", "h2", "h3", "h4", "h5", "h6"}
# page chrome: <head> holds the <title>, which repeats headline numbers next to site banners
SKIP_TAGS = {"head", "script", "style", "noscript", "nav", "header", "footer", "aside", "svg", "form"}
JUNK_HOSTS = {"twitter.com", "x.com", "facebook.com", "linkedin.com", "reddit.com",
              "pinterest.com", "whatsapp.com", "t.me", "bsky.app", "threads.net",
              "google.com", "news.google.com",
              # catalog and metadata pages: they describe a source but never contain its text
              "worldcat.org", "scixplorer.org", "api.semanticscholar.org", "wikidata.org"}
DATE_META = {"article:published_time", "datepublished", "citation_publication_date", "citation_date",
             "citation_online_date", "dc.date", "dc.date.issued", "dcterms.date", "dcterms.issued", "date",
             "pubdate", "publish-date", "publication_date", "sailthru.date", "parsely-pub-date"}
REPORT_LINK = re.compile(r"\b(download|full report|read the (full )?report|view (the )?report|full text)\b", re.I)
DOI = re.compile(r"\b(10\.\d{4,9}/[^\s?#\"<>]+)")
OPENALEX = "https://api.openalex.org/works/doi:"  # free index of papers, including their free copies
# terminal control codes and text-direction overrides: page text could use them to recolour, rewrite
# or disguise what's printed
UNSAFE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")
FAKE_IPS = ipaddress.ip_network("198.18.0.0/15")  # VPN and proxy apps answer every name with these
MAX_BYTES = 20_000_000  # the most of any one page or PDF to read, before and after decompressing
OLD_YEARS = 5  # an origin older than this gets a 'check for a newer figure' warning
OCR_PAGES, OCR_SECONDS = 15, 60  # scanned PDFs: read at most this many pages, for at most this long
STOPWORDS = set("that this with from were have has been their they about into than more most "
                "also over said says percent which what when where there these those".split())


class Page(HTMLParser):
    """Splits a page into text blocks, each with the links inside it."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks = [["", []]]  # [text, hrefs]
        self.pos = [[]]           # per block, where each href starts in the block's text
        self.ids = {}             # element id -> block index (for footnotes like #cite_note-3)
        self.skip = 0
        self.data_script = None   # collecting a JSON data script (see json_blocks)
        self.data = []
        self.dates = []           # publication dates from <meta> and <time> tags
        self.report_links = []    # [(href, says_report)]: 'Download the full report' links and PDFs
        self.anchor = None        # [href, text] of the <a> being read
        self.scanned = False      # a PDF read by OCR

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "script" and (a.get("type") == "application/ld+json" or a.get("id") == "__NEXT_DATA__"):
            self.data_script = []
        name = (a.get("property") or a.get("name") or a.get("itemprop") or "").lower()
        if tag == "meta" and name in DATE_META and a.get("content"):
            self.dates.append(a["content"])
        if tag == "time" and a.get("datetime"):
            self.dates.append(a["datetime"])
        if tag in SKIP_TAGS:
            self.skip += 1
            return
        if tag in BLOCK_TAGS:
            self.blocks.append(["", []])
            self.pos.append([])
        if a.get("id"):
            self.ids[a["id"]] = len(self.blocks) - 1
        if tag == "a" and a.get("href") and not self.skip:
            self.blocks[-1][1].append(a["href"])
            self.pos[-1].append(len(self.blocks[-1][0]))
            self.anchor = [a["href"], ""]

    def sentence_links(self, i, pat):
        """The links in block i that belong to the sentence stating the number, plus the footnote
        markers right after it ('obese.[5][6]'). Long paragraphs cite different sources for
        different sentences, so the rest of the paragraph's links are about something else.
        Falls back to all the block's links when positions are unknown or none are in the sentence."""
        text, hrefs = self.blocks[i]
        pos = self.pos[i] if i < len(self.pos) else []
        m = pat.search(text) if pat else None
        if not m or len(pos) != len(hrefs):
            return hrefs
        ends = [e.start() for e in re.finditer(r"[.!?](?=[\s\[]|$)", text)]
        cluster = r"[.!?]?(?:\s*\[[^\]]{1,20}\])*"  # a full stop and its footnote markers
        start = max([e for e in ends if e < m.start()], default=None)
        # start after the previous sentence's full stop and its footnote markers
        start = 0 if start is None else start + len(re.match(cluster, text[start:]).group())
        end = min([e for e in ends if e >= m.end()], default=len(text))
        end += len(re.match(cluster, text[end:]).group())
        inside = [h for h, p in zip(hrefs, pos) if start <= p <= end]
        return inside or hrefs

    def handle_endtag(self, tag):
        if tag == "script" and self.data_script is not None:
            self.data.append("".join(self.data_script))
            self.data_script = None
        if tag in SKIP_TAGS and self.skip:
            self.skip -= 1
        if tag == "a" and self.anchor:
            href, text = self.anchor
            says_report = bool(REPORT_LINK.search(text))
            if says_report or urlparse(href).path.lower().endswith(".pdf") or re.search(r"\bpdf\b", text, re.I):
                self.report_links.append((href, says_report))
            self.anchor = None

    def handle_data(self, data):
        if self.data_script is not None:
            self.data_script.append(data)
        elif not self.skip:
            self.blocks[-1][0] += data
            if self.anchor:
                self.anchor[1] += data

    def date(self, url=""):
        """When the page was published, as 'YYYY-MM-DD', 'YYYY-MM' or 'YYYY', or ''."""
        found = self.dates + re.findall(r'"datePublished"\s*:\s*"([^"]+)"', " ".join(self.data))
        in_url = re.search(r"/((?:19|20)\d\d)/(0[1-9]|1[0-2])/", url)  # /2024/09/18/story
        if in_url:
            found.append(f"{in_url[1]}-{in_url[2]}")
        for d in found:
            m = re.match(r"\s*((?:19|20)\d\d)(?:[-/](\d\d)(?:[-/](\d\d))?)?", d)
            if m:
                return "-".join(p for p in m.groups() if p)
        return ""

    def json_blocks(self):
        """Pages built by JavaScript often ship their article as JSON (schema.org articleBody,
        Next.js __NEXT_DATA__). Returns its long strings as blocks, parsing any HTML inside."""
        out = []

        def walk(v):
            if isinstance(v, dict):
                for x in v.values():
                    walk(x)
            elif isinstance(v, list):
                for x in v:
                    walk(x)
            elif isinstance(v, str) and len(v) >= 40 and " " in v:
                if "<" in v and ">" in v:
                    sub = Page()
                    sub.feed(v)
                    out.extend(sub.blocks)
                else:
                    out.append([v, re.findall(r"https?://[^\s<>\"')\]]+", v)])
        for raw in self.data:
            try:
                walk(json.loads(raw))
            except (ValueError, RecursionError):  # broken, or nested deeper than Python can follow
                pass
        return out


ONES = ("zero one two three four five six seven eight nine ten eleven twelve thirteen "
        "fourteen fifteen sixteen seventeen eighteen nineteen").split()
TENS = "twenty thirty forty fifty sixty seventy eighty ninety".split()
WORDNUM = {w: i for i, w in enumerate(ONES)} | {w: 20 + 10 * i for i, w in enumerate(TENS)}
FRACTION = {"half": 50, "third": 100 / 3, "quarter": 25, "fourth": 25, "fifth": 20, "tenth": 10}
_WORD = rf"\b(?:(?:{'|'.join(TENS)})(?:[- ](?:{'|'.join(ONES[1:10])}))?|{'|'.join(ONES)})\b"
_NUM = rf"(?:\d{{1,3}}(?:,\d{{3}})+(?:\.\d+)?|\d+(?:\.\d+)?|{_WORD})"  # 35,000 | 21.7 | twenty-five
QTY = re.compile(
    rf"(?<![\d.])(?:"
    rf"(?P<n>{_NUM})\s+(?:in|out of)\s+(?:every\s+)?(?P<d>{_NUM})\b"           # one in five, 3 out of 4
    rf"|(?P<frac>\b(?:(?:a|an|one|two|three)[- ])?(?:half|thirds?|quarters?|fourths?|fifths?|tenths?))\s+of\b"
    rf"|(?P<num>{_NUM})(?:(?P<pct>\s*(?:%|percent\b|per[- ]cent\b))"            # 25%, twenty-five percent
    # 368 million, $4.99M; but not 5G or 3rd
    rf"|(?P<scale>\s*(?:thousand|million|billion|trillion|bn|mn)\b|(?-i:[KMB])\b))?(?!\.?\d)(?![A-Za-z])"
    rf")", re.I)
SCALE = {"thousand": 1e3, "million": 1e6, "billion": 1e9, "bn": 1e9, "trillion": 1e12, "mn": 1e6,
         "k": 1e3, "m": 1e6, "b": 1e9}


def to_number(s):
    s = s.lower()
    return float(s.replace(",", "")) if s[0].isdigit() else sum(WORDNUM[p] for p in re.split(r"[- ]", s))


def quantities(text):
    """Yields (match, value, is_share, is_approx) for every number-like phrase in text.
    Shares ('25%', 'a quarter of', 'one in four') are all in percent."""
    for m in QTY.finditer(text):
        if m["n"]:
            d = to_number(m["d"])
            if d:
                yield m, to_number(m["n"]) / d * 100, True, True
        elif m["frac"]:
            *count, unit = re.split(r"[- ]", m["frac"].lower())  # 'two-thirds' -> ['two'], 'thirds'
            yield m, (WORDNUM.get(count[0], 1) if count else 1) * FRACTION[unit.rstrip("s")], True, True
        else:
            scale = SCALE[m["scale"].strip().lower()] if m["scale"] else 1
            yield m, to_number(m["num"]) * scale, bool(m["pct"]), False


def is_year(m):
    """'2019' in 'In 2019, 368 million tonnes' is a date, not the statistic."""
    return bool(m["num"] and not m["pct"] and not m["scale"] and re.fullmatch(r"(1[6-9]|20)\d\d", m["num"]))


CHANGE_AFTER = re.compile(r"\s*\)?\s*(increase|decrease|rise|drop|decline|growth|jump|fall|reduction|more|less|"
                          r"fewer|higher|lower|faster|slower|longer|shorter|year-over-year|yoy|annually|"
                          r"over\s+(the\s+)?(last|past|previous))\b|\s*\)?\s*(from|since)\s+\$?\d", re.I)
CHANGE_BEFORE = re.compile(r"\b(increased?|decreased?|rose|grew|grown|jumped|fell|dropped|declined|climbed|surged|"
                           r"plunged|reduced|cut|up|down|rise|growth|increase of|decrease of|drop of|rise of)"
                           r"\s+(by\s+)?(about|nearly|almost|over|more than|roughly|around|an?)?\s*$", re.I)


def kind(text, m):
    """What a percentage measures: 'share' (40% of employees), 'change' (a 40% increase, up 12%,
    50% from 2019), or 'noise' (95% confidence interval). Other numbers are 'count'."""
    if not (m["pct"] or m["n"] or m["frac"]):
        return "count"
    after = text[m.end():m.end() + 30]
    if re.match(r"\s*(confiden|CI\b|UI\b|credible)", after, re.I):
        return "noise"
    if m["pct"] and (CHANGE_AFTER.match(after) or CHANGE_BEFORE.search(text[max(0, m.start() - 30):m.start()])):
        return "change"
    return "share"


def range_bound(text, m):
    """True for an end of an uncertainty range in brackets: '929,000 (660,000–1,270,000) deaths'."""
    return bool(re.search(r"\(\s*[\d.,]+\s*[–-]\s*$", text[max(0, m.start() - 20):m.start()])
                or re.match(r"\s*[–-]\s*[\d.,]+\s*\)", text[m.end():m.end() + 20])
                and re.search(r"\(\s*$", text[max(0, m.start() - 3):m.start()]))


class Quantity:
    """The claim's number. .search(text) finds the same amount however it's written:
    '25%' matches '25 percent', 'a quarter of', 'one in four', but not '25.5%' or '125%';
    '368 million' matches '368,000,000'."""

    def __init__(self, claim, number=None):
        """number picks one when the claim has several ('$4.99 million, up 12%'); default: the first."""
        text = norm(number or claim)
        found = list(quantities(text))
        if not found:
            raise ValueError("the claim needs a number in it, e.g. '25% of ...' or 'one in five ...'")
        stats = [q for q in found if not is_year(q[0])]
        if not stats:
            raise ValueError("the claim's only number looks like a year; include the statistic itself")
        # the first number, unless it's a duration ('over 3 years, 25% of ...')
        m, self.value, self.share, self.approx = next(
            (q for q in stats if q[2] or not DURATION.match(text, q[0].end())), stats[0])
        self.kind = kind(text, m)
        self.group = group_word(norm(claim), self.search(norm(claim)) or m)
        # a number with 3+ significant digits (21.7%, 30903, $495.50) is too specific to match by chance
        digits = re.sub(r"\D", "", m["num"] or "")
        self.specific = len(digits.strip("0") if "." in (m["num"] or "") else digits.lstrip("0").rstrip("0")) >= 3

    def search(self, text):
        for m, value, share, approx in quantities(text):
            if not share and is_year(m) and not self.approx:
                continue  # never let a year stand in for a count of 2019 things
            if kind(text, m) != self.kind or range_bound(text, m):
                continue  # '40% of staff' isn't 'a 40% increase'; '(660,000–1,270,000)' is a margin of error
            tolerance = 0.5 if approx or self.approx else max(abs(self.value) * 1e-9, 1e-9)
            if share == self.share and abs(value - self.value) < tolerance:
                return m
        return None


DURATION = re.compile(r"\s*(years?|months?|weeks?|days?|hours?|minutes?|decades?)\b", re.I)
GROUP_FILLER = {"of", "the", "all", "u.s", "us", "uk", "american", "british", "surveyed", "polled"}


def group_word(text, m):
    """Who a number counts: 'adults' in '2 in 10 U.S. adults', 'Democrats' in '2 in 10 Democrats say'.
    shortcut: a plural-looking word (ends in s) right after the number counts as the group; good
    enough to tell groups apart, upgrade to real parsing if verbs like 'says' cause misses."""
    words = [w.lower().rstrip(".") for w in re.findall(r"[A-Za-z][A-Za-z.'’-]*", text[m.end():m.end() + 40])]
    words = [w for w in words[:4] if w not in GROUP_FILLER]
    w = words[0] if words else ""
    return w if len(w) >= 4 and w.endswith("s") and not w.endswith("ss") and w not in STOPWORDS else ""


def stems(keys):
    return {k[:5] for k in keys}


def mentions(text, keystems):
    """The claim stems that begin a word in text: 'form' counts in 'formal' but not in 'platform'."""
    words = re.findall(r"[a-z]+", text.lower())
    return {k for k in keystems if any(w.startswith(k) for w in words)}


SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"“(\[])")


def relevant(texts, i, keystems, pat):
    """True if the number in block i is about the claim. A common number like '20%' needs two claim
    words (one if the claim has only one): at least one in its own sentence, the other there or in
    the sentence before ('Two-thirds of them...'). A short block (a table cell or caption) may
    borrow them from the blocks around it, where its labels sit."""
    if pat.specific or not keystems:
        return True
    need = min(2, len(keystems))
    count = lambda s: mentions(s, keystems)
    if len(texts[i]) < 80:
        return len(count(" ".join(texts[max(0, i - 2):i + 3]))) >= need
    sents = SENTENCE.split(texts[i])
    return any(pat.search(s) and count(s) and len(count(s) | count(sents[j - 1] if j else "")) >= need
               and same_group(s, pat, keystems) for j, s in enumerate(sents))


def same_group(sentence, pat, keystems):
    """False when the number here counts another group: '2 in 10 Democrats' for '2 in 10 adults'
    (unless the sentence also mentions adults)."""
    theirs = group_word(sentence, pat.search(sentence))
    return not (pat.group and theirs and theirs[:5] not in keystems and pat.group[:5] not in sentence.lower())


def norm(text):
    """Collapses whitespace, and rewrites unambiguous European number formats the English way:
    '1 967' (non-breaking or thin space) -> '1,967', '1.967.000' -> '1,967,000', '25,5 %' -> '25.5 %',
    '1·27' -> '1.27'."""
    text = re.sub(r"(?<=\d)[\u00a0\u202f\u2009](?=\d{3}(?!\d))", ",", text)  # no-break / thin spaces
    text = re.sub(r"(?<=\d)\u00b7(?=\d)", ".", text)  # The Lancet's decimal point: 1·27 million
    text = re.sub(r"(?<![\d.,])\d{1,3}(?:\.\d{3}){2,}(?![\d.,])", lambda m: m[0].replace(".", ","), text)
    text = re.sub(r"(?<![\d.,])(\d{1,3}),(\d{1,2})(?=\s*(?:%|percent|per cent|prozent|pour cent))", r"\1.\2", text)
    return re.sub(r"\s+", " ", text).strip()


def keywords(claim):
    """The claim's topic words. The number's own words ('third', 'percent', 'million') don't count."""
    words = QTY.sub(" ", claim.lower())
    return {w for w in re.findall(r"[a-z]{4,}", words) if w not in STOPWORDS}


def snippet(text, pat, width=110):
    m = pat.search(text)
    start = max(0, (m.start() if m else 0) - width)
    return ("…" if start else "") + text[start:start + 2 * width].strip() + "…"


class NotPublic(OSError):
    pass


def check_public(url):
    """Refuses anything but a web page on the public internet. A page SourceTrace reads chooses the
    links it follows, so without this a page could make it open localhost, your router or a cloud
    metadata service (server-side request forgery).
    shortcut: checks the name's addresses before connecting, so a DNS server that answers differently
    the second time (DNS rebinding) could slip through; pin the address if this ever runs as a service."""
    parts = urlparse(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise NotPublic(f"not a web address: {url[:80]}")
    try:
        addresses = {a[4][0] for a in socket.getaddrinfo(parts.hostname, None, proto=socket.IPPROTO_TCP)}
    except (OSError, UnicodeError, ValueError):
        return  # an unknown name: the request itself reports it
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%")[0])
        if ip.version == 6 and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if not ip.is_global and ip not in FAKE_IPS:
            raise NotPublic(f"refusing to open a private network address ({parts.hostname})")


class PublicRedirects(HTTPRedirectHandler):
    """Redirects get the same check: a public page mustn't bounce us into the local network."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_public(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url):
    """Returns (kind, body, final_url). kind 'pdf' has bytes for body, 'html' has text."""
    headers = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.8",
               "Accept-Language": "en-US,en;q=0.8"}
    url = clean_url(url)
    check_public(url)
    # a cookie jar lets sites that set a cookie and redirect (consent pages) finish their redirect
    opener = build_opener(HTTPCookieProcessor(CookieJar()), PublicRedirects())
    with opener.open(Request(url, headers=headers), timeout=12) as r:
        data = decompress(r.read(MAX_BYTES), r.headers.get("Content-Encoding", ""))
        ctype = r.headers.get_content_type()  # 'text/plain' when the server sends none
        if ctype == "application/pdf" or data[:4] == b"%PDF":
            return "pdf", data, r.url
        if not (ctype.startswith("text/") or re.search(r"html|xml|json", ctype)):
            raise OSError(f"can't read {ctype} files")  # an ebook, image or zip read as text is noise
        try:
            return "html", data.decode(r.headers.get_content_charset() or "utf-8", "replace"), r.url
        except LookupError:  # a charset Python doesn't know ('utf8mb4')
            return "html", data.decode("utf-8", "replace"), r.url


def decompress(data, encoding):
    """The Wayback Machine replays pages with their original compression, even unasked. Stops at
    MAX_BYTES, so a small 'zip bomb' can't expand into gigabytes."""
    encoding = encoding.lower().strip()
    if encoding in ("", "identity", "none"):  # some servers say 'none' for uncompressed
        return data
    if encoding in ("gzip", "x-gzip", "deflate"):
        d = zlib.decompressobj(47)  # 47 = auto-detect gzip or zlib header
        out = d.decompress(data, MAX_BYTES)
        too_big = bool(d.unconsumed_tail)
    elif encoding == "zstd":
        try:
            from compression import zstd  # standard library from Python 3.14
        except ImportError:
            raise OSError("page is zstd-compressed; needs Python 3.14+") from None
        d = zstd.ZstdDecompressor()
        out = d.decompress(data, max_length=MAX_BYTES)
        too_big = not d.eof and not d.needs_input
    else:
        raise OSError(f"can't decode {encoding}-compressed page")
    if too_big:
        raise OSError(f"page is over {MAX_BYTES // 1_000_000} MB once decompressed")
    return out


def clean_url(url):
    """Drops invisible characters and percent-encodes non-ASCII ones, which urllib can't send."""
    url = re.sub(r"[​-‏  ﻿\s]", "", url)
    return quote(url, safe=":/?#[]@!$&'()*+,;=%~")


def canonical(url):
    """One address per DOI: 'dx.doi.org/10.2139/X' and 'doi.org/10.2139%2Fx' are the same paper.
    DSpace 7 repositories (FAO, WHO IRIS, universities) serve '/bitstreams/<id>/download' as a
    JavaScript app; the file itself is at '/server/api/core/bitstreams/<id>/content'."""
    if host_of(url) in ("doi.org", "dx.doi.org"):
        return "https://doi.org/" + unquote(urlparse(url).path.lstrip("/")).lower()
    return re.sub(r"^(https?://[^/?#]+)/bitstreams/([0-9a-f-]{36})/download(?=$|[?#])",
                  r"\1/server/api/core/bitstreams/\2/content", url)


def archived(url):
    """'https://web.archive.org/web/2021.../https://x.com/a' -> ('https://x.com/a', raw-snapshot URL)."""
    m = re.match(r"https?://web\.archive\.org/web/(\d+)[a-z_]*/(.+)", url)
    if not m:
        return None, None
    original = m[2] if m[2].startswith("http") else "http://" + m[2]
    return original, f"https://web.archive.org/web/{m[1]}id_/{original}"  # id_ = no toolbar


def newest_copy(url, fetcher):
    """The raw address of the Wayback Machine's newest copy of url, or None if it has none. Asks the
    quick lookup first: 'no copy' takes a second there, where fetching a missing copy can take 25.
    If the lookup itself fails, asks for the newest copy directly."""
    try:
        with ARCHIVE_SLOTS:
            found = json.loads(fetcher(AVAILABLE + quote(url, safe=""))[1])
        closest = found["archived_snapshots"].get("closest") or {}
    except Exception:
        return ARCHIVE + url
    return archived(closest["url"])[1] if closest.get("available") and closest.get("url") else None


# 'are you a robot?' pages sent as an ordinary page: Imperva, Cloudflare, DataDome, Akamai, F5
BOT_CHECK = re.compile(r"Incapsula|captcha-delivery\.com|<title>\s*(Just a moment|Attention Required|Access Denied"
                       r"|Request Rejected)", re.I)


def fetch_or_archive(url, fetcher):
    """Fetch url; if it's blocked, gone or down, try the Wayback Machine's newest copy.
    Returns (kind, body, base_url_for_links, via_archive)."""
    original, snapshot = archived(url)
    if snapshot:  # a link that already points into the archive: read the raw snapshot
        kind, body, _ = fetcher(snapshot)
        return kind, body, original, True
    try:
        kind, body, final = fetcher(url)
        if kind == "html" and len(body) < 100_000 and BOT_CHECK.search(body):
            raise OSError("blocked by a bot check")
        return kind, body, final, False
    except NotPublic:
        raise  # and don't hand a private address to the archive either
    except Exception as live_error:
        target = newest_copy(url, fetcher)
        if not target:
            raise OSError(f"{live_error}; archive: no copy") from None
        for attempt in range(2):  # the archive is often slow or briefly rate-limited
            try:
                with ARCHIVE_SLOTS:
                    kind, body, _ = fetcher(target)
                return kind, body, url, True
            except Exception as e:
                archive_error = e
            # retry only what's temporary: refused connections, rate limits, server errors;
            # not 'no snapshot' (404), 'forbidden' or a timeout, which just cost more time
            temporary = (getattr(archive_error, "code", 0) in (429, 500, 502, 503, 504)
                         or "refused" in str(archive_error).lower())
            if attempt or not temporary:
                break
            time.sleep(2)
        raise OSError(f"{live_error}; archive: {archive_error}") from None


class PdfUnreadable(Exception):
    pass


def run(cmd, timeout=60):
    return subprocess.run(cmd, capture_output=True, timeout=timeout, check=True).stdout.decode("utf-8", "replace")


def ocr(pdf_path):
    """Text of a scanned PDF's first pages, read by tesseract."""
    if not (shutil.which("tesseract") and shutil.which("pdftoppm")):
        raise PdfUnreadable("it has no text layer (probably a scan); install tesseract to read scans "
                            "(macOS: brew install tesseract; Linux: apt install tesseract-ocr), "
                            "or use the Docker image, which has it")
    stop = time.monotonic() + OCR_SECONDS
    with tempfile.TemporaryDirectory() as d:
        # -scale-to caps each image at 2000 pixels, so a PDF with giant pages can't fill the disk
        run(["pdftoppm", "-scale-to", "2000", "-l", str(OCR_PAGES), "-png", pdf_path, f"{d}/p"], timeout=OCR_SECONDS)
        return "\f".join(run(["tesseract", str(p), "-"], timeout=OCR_SECONDS)
                          for p in sorted(Path(d).glob("p*.png")) if time.monotonic() < stop)


def pdf_page(data):
    """Turns PDF bytes into a Page using poppler's pdftotext (tesseract for scans). Printed URLs count as links."""
    if not shutil.which("pdftotext"):
        raise PdfUnreadable("install poppler to read PDFs (macOS: brew install poppler; "
                            "Linux: apt install poppler-utils)")
    scanned, date = False, ""
    with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
        f.write(data)
        f.flush()
        try:
            text = run(["pdftotext", "-q", f.name, "-"])
            if not text.strip():
                text, scanned = ocr(f.name), True
        except (subprocess.SubprocessError, OSError) as e:
            raise PdfUnreadable(f"couldn't read it ({e})") from None
        try:  # the date is a nice-to-have; a PDF without one still gets read
            info = re.search(r"^CreationDate:\s*((?:19|20)\d\d-\d\d-\d\d)", run(["pdfinfo", "-isodates", f.name]), re.M)
            date = info[1] if info else ""
        except (subprocess.SubprocessError, OSError):
            pass
    # pdftotext breaks long links across lines ('https://cloud.google.com/b' + 'igquery').
    # shortcut: a link ending a line followed by a lowercase word gets glued to it; that link then
    # fails to open and falls back harmlessly. Upgrade if PDFs with such text show up a lot.
    prev = None
    while prev != text:
        prev, text = text, re.sub(r"(https?://\S+)\n(?![A-Z\[])([\w\-./?=&%#~:]+)", r"\1\2", text)
    page = Page()
    # a paragraph ends at a blank line or page break; join lines and undo end-of-line hyphenation
    page.blocks = [[re.sub(r"(?<=[a-z])-\n(?=[a-z])", "", b),
                    [u.rstrip(".,;:") for u in re.findall(r"https?://[^\s<>\"')\]]+", b)]]
                   for b in re.split(r"\n\s*\n|\f", text) if b.strip()]
    if not any(t.strip() for t, _ in page.blocks):
        raise PdfUnreadable("no readable text, even with OCR")
    page.scanned = scanned
    page.dates = [date] if date else []
    return page


def cited_links(page, hit_blocks, base, near=False, pat=None):
    """Links inside the blocks that state the number, footnotes resolved, outside sites first."""
    inline, notes = [], []
    for i in hit_blocks:
        for href in page.sentence_links(i, None if near else pat):
            if href.startswith("#"):  # footnote: take links from the note it points to
                j = page.ids.get(href[1:])
                if j is not None:  # only that note; the next block is usually another note
                    note = [h for h in page.blocks[j][1] if not h.startswith("#")]
                    if not note and j + 1 < len(page.blocks):  # note text sits in a nested block
                        note = [h for h in page.blocks[j + 1][1] if not h.startswith("#")]
                    notes += note
            else:
                inline.append(href)
        # numbered citations like [12]: take links from the reference entry that starts with [12]
        for n in re.findall(r"\[(\d{1,3})\]", page.blocks[i][0]):
            notes += [h for t, hrefs in page.blocks if t.lstrip().startswith(f"[{n}]") for h in hrefs]
    here = host_of(base)
    if notes:  # with footnotes, links to the same site are navigation (Wikipedia's 'doi', article links)
        inline = [h for h in inline if host_of(urljoin(base, h)) != here]
        outside = [h for h in notes if host_of(urljoin(base, h)) != here]
        notes = outside or notes
    seen, links = set(), []
    for href in notes + inline:
        url = canonical(urldefrag(urljoin(base, href))[0])
        if url in seen or url == urldefrag(base)[0] or not url.startswith("http") or is_junk(url, base):
            continue
        seen.add(url)
        links.append(url)
    # an archive copy of a link that's also cited directly is the same source; the direct link
    # falls back to the archive by itself if it's dead
    links = [u for u in links if archived(u)[0] not in seen]
    links.sort(key=lambda u: host_of(archived(u)[0] or u) == here)  # external citations first
    if not links and not near:
        # Many articles cite a paragraph or two away from the number. Only look at real
        # prose paragraphs nearby, so share buttons and logo strips don't count as sources.
        window = sorted({j for i in hit_blocks for j in range(max(0, i - 6), min(i + 7, len(page.blocks)))
                         if len(norm(page.blocks[j][0])) >= 60})
        return cited_links(page, window, base, near=True)
    return links


def host_of(url):
    return urlparse(url).netloc.lower().removeprefix("www.")


def same_site(a, b):
    """'www.cdc.gov' and 'stacks.cdc.gov' are one site; 'nhlbi.nih.gov' is another.
    shortcut: compares the last two parts of the name, so two 'x.co.uk' sites count as one."""
    return host_of(a).split(".")[-2:] == host_of(b).split(".")[-2:]


def is_junk(url, base):
    """Share buttons, social sites, homepages, same-site menu links, and links that embed the page's own address."""
    host = host_of(url)
    parts = [p for p in urlparse(url).path.split("/") if p]
    # a source is a specific page: '/' is a homepage, and a short one-part path on the same
    # site ('/research', '/about') is a section menu; '/research/oss-development-gen-ai-era' counts
    shallow = (not parts or (host == host_of(base) and len(parts) == 1 and len(parts[0]) < 25)
               or (len(parts) == 1 and re.fullmatch(r"[a-z]{2}([-_][a-z]{2})?", parts[0], re.I)))  # /en, /en-us
    return (shallow or any(host == j or host.endswith("." + j) for j in JUNK_HOSTS)
            or re.search(r"/(intent|share|sharer|send)\b", url) is not None
            or SITE_PAGE.search(urlparse(url).path) is not None
            or quote(urldefrag(base)[0], safe="") in url or urldefrag(base)[0] in url.split("?", 1)[-1])


# site pages, not sources: /contact/, /register/apptrana-free-trial, /privacy-policy
SITE_PAGE = re.compile(r"/(contact(-us)?|register|sign-?up|log-?in|subscribe|pricing|careers|privacy(-policy)?"
                       r"|terms(-of-[a-z]+)?|case-studies|[\w-]*free-trial)(/|$)", re.I)


def load(url, fetcher):
    """fetch_or_archive, but errors become a result: one dead source shouldn't stop the trace."""
    try:
        return fetch_or_archive(url, fetcher)
    except NotPublic as e:
        return ("error", str(e), url, False)
    except Exception as e:  # network errors, 403s, timeouts, bot walls
        return ("error", f"could not open it or an archived copy ({str(e)[:160]})", url, False)


def read_page(kind, body):
    """Page from fetched content. Raises PdfUnreadable for PDFs we can't get text from."""
    if kind == "pdf":
        return pdf_page(body)
    page = Page()
    page.feed(body)
    if sum(len(t.strip()) for t, _ in page.blocks) < 5000:  # thin page: probably built by JavaScript
        page.blocks += page.json_blocks()
    return page


# '[12] ...', '12. Smith, J. ...', 'Gunders, D. 2012. Wasted: ...', 'Smith J, Doe A. Title. JAMA 2016;...'
REFERENCE = re.compile(r"\s*(\[\d{1,3}\]|\d{1,3}\.\s+[A-Z][\w'’-]+,|[A-Z][\w'’-]+,(\s+[A-Z]\.)+"
                       r"|[A-Z][\w'’-]+ [A-Z]{1,2}, [A-Z][\w'’-]+ [A-Z]{1,2}[,.])")


def different_number(texts, keystems, pat):
    """The sentence most clearly about the claim, if it gives a different number for the same thing.
    That's evidence the number changed, not just went missing, so it's strict: the sentence has two
    claim words, and the other number is of the same kind (a share for a share, a count within 10x),
    with a claim word right beside it ('68% of Democrats', 'Democrats (68%)'), and doesn't count another group."""
    if len(keystems) < 2:
        return None
    near = lambda s, m: " ".join(s[:m.start()].split()[-3:] + s[m.end():].split()[:3]).lower()
    best, score = None, max(2, (len(keystems) + 1) // 2) - 1  # half the claim's words, at least 2
    for t in texts:
        for s in SENTENCE.split(t):
            n = len(mentions(s, keystems))
            if n > score and any(
                    share == pat.share and kind(s, m) == pat.kind and not is_year(m) and not pat.search(m[0])
                    and not s[m.end():m.end() + 1].isalpha()  # '5G', not 5
                    and (share or 0.1 < value / (pat.value or 1) < 10)
                    and mentions(near(s, m), keystems)
                    and (not pat.group or group_word(s, m)[:5] in ("", pat.group[:5]))  # not '81% of workplaces'
                    for m, value, share, _ in quantities(s)):
                best, score = s, n
    return best


def time_left(budget):
    """Seconds left, or None for no time limit. budget is [pages left] or [pages left, deadline],
    the deadline a time.monotonic() value."""
    return max(budget[1] - time.monotonic(), 0) if len(budget) > 1 else None


def open_copy(url, fetcher):
    """A free full-text copy of a paper (arXiv, PubMed Central, the author's site), found through
    OpenAlex. Paywalled journals show only an abstract or block us outright."""
    doi = DOI.search(unquote(url))
    if not doi:
        return None
    try:
        work = json.loads(fetcher(OPENALEX + quote(doi[1].rstrip(".").lower(), safe="/"))[1])
    except Exception:  # no record, rate-limited, offline: then there's simply no copy to offer
        return None
    for loc in [work.get("best_oa_location")] + (work.get("locations") or []):
        for u in (loc or {}).get("pdf_url"), (loc or {}).get("landing_page_url"):
            if u and (loc or {}).get("is_oa") and host_of(u) not in ("doi.org", "dx.doi.org") \
                    and canonical(u) != canonical(url):
                return u
    return None


def copies(url, final, page, fetcher):
    """Other places the same source keeps its text: the report a landing page links to
    ('Download the full report'), or a free copy of a paywalled paper."""
    found = []
    for href, says_report in page.report_links if page else []:
        u = canonical(urldefrag(urljoin(final, href))[0])
        # a bare PDF link elsewhere is just another document the page mentions, not its report
        if (says_report or same_site(u, final)) and u.startswith("http") and u not in (url, final) \
                and u not in found and not is_junk(u, final):
            found.append(u)
    found.sort(key=lambda u: not urlparse(u).path.lower().endswith(".pdf"))  # the PDF first
    free = open_copy(final, fetcher) or (open_copy(url, fetcher) if final != url else None)
    return ([free] if free else []) + found[:2]


def trace(url, pat, keys, fetcher, depth, max_links, path=(), cache=None, budget=None, alt=True):
    """Follows url's citations for the claim's number. Returns a tree of nodes. alt=False for a
    report or free copy of a source: it's the same source, so its own copies aren't looked for."""
    cache = {} if cache is None else cache
    budget = [30] if budget is None else budget
    keystems = stems(keys)
    node = {"url": url, "found": False, "context": "", "note": "", "status": "", "children": []}

    def done(status, note=""):
        node["status"], node["note"] = status, note
        return node

    def follow(links, d, same_source=False):
        """Fetches links all at once, then traces each from the cache."""
        todo = [u for u in links if u not in cache and u not in path and u != url][:max(budget[0], 0)]
        budget[0] -= len(todo)
        # daemon threads, so a source that's still loading when time runs out can't hold up the exit
        results = {}
        threads = [threading.Thread(target=lambda u=u: results.__setitem__(u, load(u, fetcher)), daemon=True)
                   for u in todo]
        for t in threads:
            t.start()
        for t in threads:
            t.join(time_left(budget))
        cache.update(results)  # sources not back in time are reported as skipped
        for link in links:
            child = trace(link, pat, keys, fetcher, d, max_links, path + (url,), cache, budget, alt=not same_source)
            child["copy"] = same_source
            node["children"].append(child)

    def look_elsewhere(page=None, final=url):
        """A cited page without the number: read its full report or a free copy, if there is one."""
        if alt and path and time_left(budget) != 0:
            extra = copies(url, final, page, fetcher)
            if extra:
                node["note"] += "; checked its full report or a free copy instead"
                follow(extra, depth, same_source=True)
        return node

    if url in path:
        return done("loop", "LOOP: cites a page earlier in this chain")
    if url not in cache:
        if budget[0] <= 0 or time_left(budget) == 0:
            return done("skipped", "skipped (page or time limit reached)")
        budget[0] -= 1
        cache[url] = load(url, fetcher)
    kind, body, final, node["archived"] = cache[url]
    if kind == "error":
        done("unreachable", body)
        return look_elsewhere()
    try:
        page = read_page(kind, body)
    except PdfUnreadable as e:
        return done("pdf", f"PDF: open it and search for the number by hand; {e}")
    if page.scanned:
        node["scanned"] = True
    texts = [norm(t) for t, _ in page.blocks]
    raw_hits = [i for i, t in enumerate(texts) if pat.search(t)]
    hits = [i for i in raw_hits if relevant(texts, i, keystems, pat)]
    if not hits:
        # without reference-list lines: those quote other works' titles, not this source's findings
        changed = different_number([norm("\n".join(line for line in t.split("\n") if not REFERENCE.match(line)))
                                    for t, _ in page.blocks], keystems, pat)
        if changed:
            done("different", "DIFFERENT NUMBER here: " + changed[:240] + "…")
        elif raw_hits:
            done("off-topic", "the number appears, but not about the claim's topic or group")
        else:
            done("missing", "number not in this page's readable text")
        return look_elsewhere(page, final)
    node["found"] = True
    node["status"] = "found"
    node["context"] = snippet(texts[hits[0]], pat)
    node["date"] = page.date(final)
    links = cited_links(page, hits, final, pat=pat)
    if links and depth == 0:
        return done("cut", "trail continues (depth limit reached)")
    follow(links[:max_links], depth - 1)
    return node


VERDICTS = {
    "TRACED": "Found the earliest page(s) that state the number and cite nothing further for it:",
    "INCOMPLETE": "SourceTrace stopped before the trail ended (--depth, --pages or --timeout limit). "
                  "Rerun with a higher limit to follow it from:",
    "CHECK PDF": "The trail ends at PDF(s) SourceTrace couldn't read; search them for the number:",
    "DIFFERENT NUMBER": "A cited source discusses this but gives a different number. "
                        "The number may have changed as it was copied:",
    "UNVERIFIED": "The cited sources opened, but none states this number in its readable text. It may be "
                  "in a chart, table image or paywalled full text, or the citation may be wrong:",
    "UNREACHABLE": "None of the cited sources could be opened, live or archived:",
    "ORPHAN": "The page states the number but links to no source for it.",
    "NOT FOUND": "The starting page doesn't state this number about this topic.",
}


def verdict(root):
    """(label, explanation, urls): the overall finding for a traced claim."""
    if root["status"] == "unreachable":
        return "UNREACHABLE", "Couldn't open the starting page: " + root["note"], []
    if not root["found"]:
        return "NOT FOUND", VERDICTS["NOT FOUND"] + " " + root["note"], []
    by, nodes = {}, {}  # status -> urls; url -> node

    def walk(n):
        """Returns True if n or anything it cites states the number or needs a manual check."""
        deeper = False
        for c in n["children"]:
            by.setdefault(c["status"], []).append(c["url"])
            if c["found"]:
                nodes.setdefault(c["url"], c)
            deeper = walk(c) or c["status"] == "pdf" or deeper
        # an origin states the number, isn't the start page, and nothing it cites states it again
        if n is not root and n["status"] == "found" and not deeper:
            by.setdefault("origin", []).append(n["url"])
        return deeper or n["found"]
    walk(root)
    cut = ([root["url"]] if root["status"] == "cut" else []) + by.get("cut", []) + by.get("skipped", [])
    if not root["children"] and not cut:
        return "ORPHAN", VERDICTS["ORPHAN"], []
    if by.get("origin"):
        urls = list(dict.fromkeys(by["origin"]))
        return "TRACED", VERDICTS["TRACED"] + "".join("\n  " + origin_line(nodes[u]) for u in urls), urls
    for label, urls in [("INCOMPLETE", cut), ("CHECK PDF", by.get("pdf")),
                        ("DIFFERENT NUMBER", by.get("different")),
                        ("UNVERIFIED", by.get("missing", []) + by.get("off-topic", []))]:
        if urls:
            return label, VERDICTS[label] + "\n  " + "\n  ".join(dict.fromkeys(urls)), list(dict.fromkeys(urls))
    return "UNREACHABLE", VERDICTS["UNREACHABLE"] + "\n  " + "\n  ".join(by.get("unreachable", [])), \
        by.get("unreachable", [])


def origin_line(n):
    """'https://...  (published 2009-05-01; that's 17 years ago, check for a newer figure)'"""
    date = n.get("date")
    if not date:
        return n["url"] + "  (publication date unknown)"
    age = time.localtime().tm_year - int(date[:4])
    old = f"; that's {age} years ago, check for a newer figure" if age > OLD_YEARS else ""
    return f"{n['url']}  (published {date}{old})"


def render(node, indent=""):
    mark = "✔" if node["found"] else "✘"
    tags = [t for k, t in [("copy", "full report or free copy of the source above"),
                           ("archived", "read from Wayback Machine archive"), ("scanned", "scanned PDF, read by OCR"),
                           ("date", f"published {node.get('date')}")] if node.get(k)]
    lines = [f"{indent}{mark} {node['url']}" + "".join(f"  [{t}]" for t in tags)]
    if node["context"]:
        lines.append(f"{indent}    \"{node['context']}\"")
    if node["note"]:
        lines.append(f"{indent}    {node['note']}")
    for c in node["children"]:
        lines += render(c, indent + "    ")
    return lines


def all_nodes(node):
    yield node
    for c in node["children"]:
        yield from all_nodes(c)


def origins(root, urls):
    """For each origin: its URL, publication date and the passage that states the number, so an AI
    agent can check the passage means the same as the claim."""
    found = {}
    for n in all_nodes(root):
        if n["found"]:
            found.setdefault(n["url"], n)
    return [{"url": u, "date": found[u].get("date", ""), "quote": found[u]["context"]} for u in urls]


def main(argv=None):
    ap = argparse.ArgumentParser(description="Trace a statistic back to its original source.")
    ap.add_argument("claim", help='the claim, including its number, e.g. "25%% of PRs are AI slop"')
    ap.add_argument("url", help="page where you saw the claim")
    ap.add_argument("--number", help="which number in the claim to trace, if it has several (e.g. --number 12%%)")
    ap.add_argument("--depth", type=int, default=3, help="how many citation hops to follow (default 3)")
    ap.add_argument("--links", type=int, default=5, help="max citations to follow per page (default 5)")
    ap.add_argument("--pages", type=int, default=30, help="max pages to fetch in total (default 30)")
    ap.add_argument("--timeout", type=float, default=60,
                    help="stop fetching new pages after this many seconds (default 60)")
    ap.add_argument("--json", action="store_true", help="print the result as JSON (for scripts and AI agents)")
    ap.add_argument("--version", action="version", version=f"sourcetrace {__version__}")
    a = ap.parse_args(argv)
    if min(a.depth, a.links, a.pages) < 0 or a.timeout <= 0:
        ap.error("--depth, --links and --pages can't be negative, and --timeout must be above 0")
    if "://" not in a.url:
        a.url = "https://" + a.url
    try:
        pat = Quantity(a.claim, a.number)
    except ValueError as e:
        ap.error(str(e))
    root = trace(a.url, pat, keywords(a.claim), fetch, a.depth, a.links, budget=[a.pages, time.monotonic() + a.timeout])
    label, why, urls = verdict(root)
    if a.json:
        out = json.dumps({"claim": a.claim, "url": a.url, "verdict": label, "explanation": why.split("\n")[0],
                          "sources": urls, "origins": origins(root, urls) if label == "TRACED" else [],
                          "trace": root}, indent=2, ensure_ascii=False)
    else:
        out = "\n".join(render(root)) + f"\n\nVERDICT: {label}\n{why}"
    print(UNSAFE.sub("", out))
    return 0 if label == "TRACED" else 1


if __name__ == "__main__":
    sys.exit(main())
