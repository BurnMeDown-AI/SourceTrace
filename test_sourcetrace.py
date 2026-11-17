import shutil
import unittest
from urllib.error import HTTPError

from sourcetrace import ARCHIVE, AVAILABLE, OPENALEX, REFERENCE, Quantity, canonical, keywords, norm, trace, verdict


def make_pdf(*lines):
    """A minimal one-page PDF with the given text lines, so tests need no PDF library."""
    text = "BT /F1 11 Tf 50 750 Td 14 TL " + " ".join(f"({l}) '" for l in lines) + " ET"
    objs = ["<< /Type /Catalog /Pages 2 0 R >>",
            "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            "/Resources << /Font << /F1 5 0 R >> >> >>",
            f"<< /Length {len(text)} >>\nstream\n{text}\nendstream",
            "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = "%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n" + "".join(f"{o:010d} 00000 n \n" for o in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    return out.encode()


def scanned_pdf(*lines):
    """An image-only PDF (no text layer), like a scan: make_pdf rendered to a JPEG, wrapped in a PDF."""
    import subprocess, tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as d:
        Path(d, "t.pdf").write_bytes(make_pdf(*lines))
        subprocess.run(["pdftoppm", "-r", "150", "-jpeg", "-singlefile", f"{d}/t.pdf", f"{d}/img"], check=True)
        jpg = Path(d, "img.jpg").read_bytes()
    w, h = 1275, 1650  # 8.5 x 11 inches at 150 dpi
    content = b"q 612 0 0 792 0 0 cm /Im0 Do Q"
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            b"/Resources << /XObject << /Im0 5 0 R >> >> >>",
            b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
            b"<< /Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace /DeviceRGB "
            b"/BitsPerComponent 8 /Filter /DCTDecode /Length %d >>\nstream\n" % (w, h, len(jpg)) + jpg + b"\nendstream"]
    out, offsets = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1) + b"".join(b"%010d 00000 n \n" % o for o in offsets)
    return out + b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)


def fake_web(pages):
    def fetcher(url):
        if url not in pages:
            raise HTTPError(url, 404, "Not Found", {}, None)
        body = pages[url]
        return ("pdf", body, url) if isinstance(body, bytes) else ("html", body, url)
    return fetcher


def run(claim, pages, start="https://blog.example/post"):
    root = trace(start, Quantity(claim), keywords(claim), fake_web(pages), 3, 5)
    return root, verdict(root)[0]


def finds(claim, text):
    return Quantity(claim).search(norm(text)) is not None


class Matching(unittest.TestCase):
    def test_same_number_only(self):
        for yes in ["about 25% of", "25 percent", "25.0 %", "(25%)", "25 per cent"]:
            self.assertTrue(finds("25% of PRs", yes), yes)
        for no in ["125%", "25.5%", "2.25%", "25 PRs", "in 2025"]:
            self.assertFalse(finds("25% of PRs", no), no)

    def test_numbers_written_as_words(self):
        for yes in ["a quarter of incoming PRs", "one in four PRs", "1 out of 4", "twenty-five percent",
                    "one-quarter of them"]:
            self.assertTrue(finds("25% of PRs", yes), yes)
        self.assertTrue(finds("one in five adults", "20% of adults"))
        self.assertTrue(finds("33% of users", "a third of users"))
        self.assertTrue(finds("two-thirds of voters", "67% of voters"))
        self.assertTrue(finds("half of devs", "50% of devs"))
        self.assertFalse(finds("25% of PRs", "a third of PRs"))
        self.assertFalse(finds("25% of PRs", "someone in fourteen"))

    def test_plain_numbers_and_commas(self):
        self.assertTrue(finds("35,000 emails exposed", "35000 emails"))
        self.assertTrue(finds("twelve boats", "12 boats"))
        self.assertFalse(finds("35,000 emails exposed", "135,000"))

    def test_prefers_share_over_year(self):
        self.assertTrue(finds("In 2026, 40% of devs", "40 percent"))

    def test_european_number_formats(self):
        self.assertTrue(finds("25.5% of firms", "25,5 % of firms"))
        self.assertTrue(finds("1,967 repos", "1\u00a0967 repos"))
        self.assertTrue(finds("1,967 repos", "1\u202f967 repos"))
        self.assertTrue(finds("1,967,000 repos", "1.967.000 repos"))
        self.assertFalse(finds("1.967 repos", "1,967 repos"))  # English comma stays a thousands separator

    def test_picks_the_first_number(self):
        self.assertEqual(Quantity("$4.99 million in 2026, up 12% from $4.44 million").value, 4.99e6)
        self.assertEqual(Quantity("$4.99 million in 2026, up 12% from $4.44 million", "12%").value, 12)
        self.assertEqual(Quantity("over 3 years, 25% of firms left").value, 25)

    def test_shorthand_scales(self):
        self.assertTrue(finds("$4.99 million", "costs reached $4.99M"))
        self.assertTrue(finds("$6.29 billion", "a $6.29B market"))
        self.assertTrue(finds("500,000 users", "500K users"))
        self.assertFalse(finds("5 people", "5G phones"))

    def test_a_change_is_not_a_share(self):
        self.assertFalse(finds("40% of employees felt burnout", "burnout saw a 40% increase"))
        self.assertFalse(finds("50% from 2019 to 2021", "half of those deaths"))
        self.assertTrue(finds("a 3.1% decrease in injuries", "injuries were down 3.1 percent from 2023"))
        self.assertTrue(finds("up 12% from last year", "a 12% increase over last year"))
        self.assertFalse(finds("95% of employees", "95% confidence interval"))
        self.assertTrue(finds("60% of voters approved it", "the measure was approved by 60% of voters"))
        self.assertTrue(finds("down 40%", "sales fell by 40% last year"))
        self.assertFalse(finds("95% of employees", "the range we are 95% confident contains it"))
        self.assertTrue(finds("1.27 million deaths", "1\u00b727 million deaths"))

    def test_margin_of_error_is_not_the_number(self):
        self.assertFalse(finds("1,270,000 deaths", "929,000 (660,000–1,270,000) deaths"))
        self.assertTrue(finds("929,000 deaths", "929,000 (660,000–1,270,000) deaths"))

    def test_needs_a_number(self):
        with self.assertRaises(ValueError):
            Quantity("lots of slop")

    def test_years_are_not_the_statistic(self):
        self.assertTrue(finds("In 2019, 368 million tonnes of plastic", "368,000,000 tonnes"))
        self.assertFalse(finds("2,000 respondents", "founded in 2000"))
        with self.assertRaises(ValueError):
            Quantity("In 1990 General Motors introduced")

    def test_scale_words(self):
        self.assertTrue(finds("$495.50 billion in 2023", "495,500,000,000 dollars"))
        self.assertTrue(finds("1.4 billion people", "1400 million people"))
        self.assertFalse(finds("1.4 billion people", "1.4 million people"))

    def test_specific_numbers(self):
        self.assertTrue(Quantity("30,903 responses").specific)
        self.assertTrue(Quantity("21.7% of packages").specific)
        self.assertFalse(Quantity("20% of submissions").specific)
        self.assertFalse(Quantity("15,000 developers").specific)


class Trace(unittest.TestCase):
    def test_traced_through_footnote(self):
        _, v = run("25% of pull requests are slop", {
            "https://blog.example/post": '<p>A quarter, 25% of pull requests<a href="#fn1">1</a></p>'
                                         '<ol><li id="fn1"><a href="https://news.example/a">src</a></li></ol>',
            "https://news.example/a": '<p>Survey: 25 percent of pull requests <a href="https://lf.example/s">here</a></p>',
            "https://lf.example/s": "<p>Maintainers estimate a quarter of incoming pull requests are slop.</p>",
        })
        self.assertEqual(v, "TRACED")

    def test_numbered_reference(self):
        _, v = run("12% rise", {
            "https://blog.example/post": "<p>Costs saw a 12% rise [3].</p><p>[3] Report, "
                                         '<a href="https://gov.example/r">gov</a></p>',
            "https://gov.example/r": "<p>a 12 percent rise</p>",
        })
        self.assertEqual(v, "TRACED")

    def test_citation_in_nearby_paragraph(self):
        _, v = run("31% on larger projects", {
            "https://blog.example/post": '<p>Slop rises to 31% on larger projects.</p><p>More text.</p>'
                                         '<p>The foundation published <a href="https://lf.example/s">the full survey</a> '
                                         'with its methodology and sample size.</p>'
                                         '<p><a href="https://x.com/intent/post?u=1">share</a></p>',
            "https://lf.example/s": "<p>31% on larger projects</p>",
        })
        self.assertEqual(v, "TRACED")

    def test_skips_banners_homepages_and_menus(self):
        root, v = run("25% of pull requests", {
            "https://blog.example/post":
                "<html><head><title>25% of pull requests are slop</title></head><body>"
                '<header><a href="https://events.example/conf">Big conference! Register now for the '
                "annual event in Salt Lake City</a></header>"
                "<h1>Survey: 25% of pull requests are slop</h1>"
                '<p>Read the <a href="https://blog.example/research/genai-survey-2026">full report</a>, '
                'from <a href="https://www.sponsor.example/en">our sponsor</a> and '
                '<a href="https://blog.example/research">all research</a>, with methodology.</p></body></html>',
            "https://blog.example/research/genai-survey-2026": "<p>1 in 4 pull requests are slop</p>",
        })
        self.assertEqual(v, "TRACED")
        self.assertEqual([c["url"] for c in root["children"]], ["https://blog.example/research/genai-survey-2026"])

    def test_dead_link_uses_archive(self):
        root, v = run("12% rise", {
            "https://blog.example/post": '<p>a 12% rise <a href="https://gone.example/r">r</a></p>',
            ARCHIVE + "https://gone.example/r": "<p>Prices: a 12% rise in March.</p>",
        })
        self.assertEqual(v, "TRACED")
        self.assertTrue(root["children"][0]["archived"])

    def test_bot_check_page_uses_archive(self):
        root, v = run("12% rise", {
            "https://blog.example/post": '<p>a 12% rise <a href="https://walled.example/r">r</a></p>',
            "https://walled.example/r": "<html><head><title>Just a moment...</title></head><p>Checking</p></html>",
            ARCHIVE + "https://walled.example/r": "<p>Prices: a 12% rise in March.</p>",
        })
        self.assertEqual(v, "TRACED")
        self.assertTrue(root["children"][0]["archived"])

    def test_same_number_on_another_topic_is_not_a_match(self):
        root, v = run("20% of submissions were AI slop", {
            "https://blog.example/post": '<p>About 20% of submissions were AI slop <a href="https://dev.example/blog/x">x</a></p>',
            "https://dev.example/blog/x": "<p>The struct went from 824 to 656 bytes: slightly over 20%.</p>",
        })
        self.assertEqual(v, "UNVERIFIED")
        self.assertEqual(root["children"][0]["status"], "off-topic")

    def test_footnote_uses_only_its_own_reference(self):
        root, _ = run("35% of the population classified as obese", {
            "https://en.wikipedia.example/wiki/Obesity":
                '<p>35% of the population classified as obese.<a href="#cite_note-5">[5]</a> '
                'See <a href="/wiki/Body_mass_index">BMI</a>.</p><ol>'
                '<li id="cite_note-5"><a href="https://cdc.example/data/obesity">CDC</a></li>'
                '<li id="cite_note-6"><a href="https://mayo.example/obesity/causes">Mayo</a></li></ol>',
            "https://cdc.example/data/obesity": "<p>35% of the population classified as obese</p>",
        }, start="https://en.wikipedia.example/wiki/Obesity")
        self.assertEqual([c["url"] for c in root["children"]], ["https://cdc.example/data/obesity"])

    def test_long_paragraph_uses_the_numbers_own_footnote(self):
        root, _ = run("35% of the population classified as obese", {
            "https://en.wikipedia.example/wiki/Obesity":
                '<p>Obesity is linked to many diseases.<a href="#cite_note-1">[1]</a> It costs a lot.'
                '<a href="#cite_note-2">[2]</a> In 2016, 35% of the population was classified as obese.'
                '<a href="#cite_note-5">[5]</a><a href="#cite_note-6">[6]</a> Rates vary by state.'
                '<a href="#cite_note-7">[7]</a></p><ol>'
                '<li id="cite_note-1"><a href="https://mayo.example/obesity/causes">1</a></li>'
                '<li id="cite_note-2"><a href="https://cost.example/obesity/costs">2</a></li>'
                '<li id="cite_note-5"><a href="https://cdc.example/data/obesity">5</a></li>'
                '<li id="cite_note-6"><a href="https://nih.example/obesity/2016">6</a></li>'
                '<li id="cite_note-7"><a href="https://states.example/obesity/map">7</a></li></ol>',
        }, start="https://en.wikipedia.example/wiki/Obesity")
        self.assertEqual([c["url"] for c in root["children"]],
                         ["https://cdc.example/data/obesity", "https://nih.example/obesity/2016"])

    def test_archive_links_read_raw_and_dedupe(self):
        from sourcetrace import archived
        self.assertEqual(archived("https://web.archive.org/web/20210101/https://x.example/a"),
                         ("https://x.example/a", "https://web.archive.org/web/20210101id_/https://x.example/a"))
        root, v = run("12% rise", {
            "https://blog.example/post": '<p>a 12% rise <a href="https://x.example/a">a</a> '
                                         '<a href="https://web.archive.org/web/20210101/https://x.example/a">archived</a></p>',
            "https://web.archive.org/web/2099id_/https://x.example/a": "<p>a 12% rise</p>",
        })
        self.assertEqual(v, "TRACED")
        self.assertEqual(len(root["children"]), 1)

    def test_javascript_page_json(self):
        page_json = ('{"props":{"pageProps":{"post":{"body":"<p>Our survey found 42% of developers '
                     'trust AI. <a href=\\"https://survey.example/2026/results\\">results</a></p>"}}}}')
        _, v = run("42% of developers trust AI", {
            "https://blog.example/post": f'<div id="app"></div><script id="__NEXT_DATA__" type="application/json">{page_json}</script>',
            "https://survey.example/2026/results": "<p>42% of developers trust AI output</p>",
        })
        self.assertEqual(v, "TRACED")

    def test_unreachable(self):
        _, v = run("12% rise", {"https://blog.example/post": '<p>a 12% rise <a href="https://gone.example/r/1">r</a></p>'})
        self.assertEqual(v, "UNREACHABLE")

    def test_clean_url(self):
        from sourcetrace import clean_url
        self.assertEqual(clean_url("https://x.example/caf\u00e9\u200b/a b"), "https://x.example/caf%C3%A9/ab")

    def test_orphan(self):
        _, v = run("40% of devs", {"https://blog.example/post": "<p>40% of devs agree.</p>"})
        self.assertEqual(v, "ORPHAN")

    def test_mutated_number(self):
        root, v = run("40% of developers trust AI", {
            "https://blog.example/post": '<p>40% of developers trust AI <a href="https://s.example/study">s</a></p>',
            "https://s.example/study": "<p>Only 4% of developers highly trust AI code.</p>",
        })
        self.assertEqual(v, "DIFFERENT NUMBER")
        self.assertIn("4% of developers", root["children"][0]["note"])

    def test_loop(self):
        root, _ = run("12% rise", {
            "https://blog.example/post": '<p>a 12% rise <a href="https://b.example/post2">b</a></p>',
            "https://b.example/post2": '<p>12% rise <a href="https://blog.example/post">x</a></p>',
        })
        self.assertTrue(root["children"][0]["children"][0]["note"].startswith("LOOP"))

    @unittest.skipUnless(shutil.which("pdftotext"), "poppler not installed")
    def test_reads_pdf(self):
        _, v = run("12% rise", {
            "https://blog.example/post": '<p>a 12% rise <a href="https://gov.example/r.pdf">r</a></p>',
            "https://gov.example/r.pdf": make_pdf("Annual report", "Costs saw a 12 percent rise."),
        })
        self.assertEqual(v, "TRACED")

    @unittest.skipUnless(shutil.which("pdftotext"), "poppler not installed")
    def test_pdf_links_wrapped_across_lines(self):
        root, v = run("12% rise", {
            "https://blog.example/post": '<p>a 12% rise <a href="https://gov.example/r.pdf">r</a></p>',
            "https://gov.example/r.pdf": make_pdf("Costs saw a 12 percent rise [4].", "[4] Stats office. https://stats.ex",
                                                  "ample/2026/costs-repo", "rt.html, 2026. Accessed May."),
            "https://stats.example/2026/costs-report.html": "<p>costs: a 12% rise</p>",
        })
        self.assertEqual(v, "TRACED")

    def test_unreadable_pdf_asks_for_manual_check(self):
        _, v = run("12% rise", {
            "https://blog.example/post": '<p>a 12% rise <a href="https://gov.example/r.pdf">r</a></p>',
            "https://gov.example/r.pdf": b"%PDF-1.4 not really a pdf",
        })
        self.assertEqual(v, "CHECK PDF")

    def test_depth_limit_is_incomplete_not_orphan(self):
        pages = {"https://blog.example/post": '<p>40% of devs <a href="https://s.example/study">s</a></p>',
                 "https://s.example/study": "<p>40% of devs</p>"}
        root = trace("https://blog.example/post", Quantity("40%"), set(), fake_web(pages), 0, 5)
        self.assertEqual(verdict(root)[0], "INCOMPLETE")

    def test_archive_lookup_uses_the_snapshot_it_finds(self):
        import json
        from urllib.parse import quote
        snap = "http://web.archive.org/web/20200101/https://gone.example/r"
        root, v = run("12% rise", {
            "https://blog.example/post": '<p>a 12% rise <a href="https://gone.example/r">r</a></p>',
            AVAILABLE + quote("https://gone.example/r", safe=""):
                json.dumps({"archived_snapshots": {"closest": {"available": True, "url": snap}}}),
            "https://web.archive.org/web/20200101id_/https://gone.example/r": "<p>a 12% rise in prices</p>",
        })
        self.assertEqual(v, "TRACED")

    def test_failed_archive_lookup_asks_for_the_newest_copy(self):
        from sourcetrace import newest_copy
        def fetcher(url):
            raise TimeoutError("timed out")
        self.assertEqual(newest_copy("https://gone.example/r", fetcher), ARCHIVE + "https://gone.example/r")

    def test_archive_lookup_with_no_copy_is_unreachable(self):
        from urllib.parse import quote
        _, v = run("12% rise", {
            "https://blog.example/post": '<p>a 12% rise <a href="https://gone.example/r">r</a></p>',
            AVAILABLE + quote("https://gone.example/r", safe=""): '{"archived_snapshots": {}}',
        })
        self.assertEqual(v, "UNREACHABLE")

    def test_time_limit_stops_the_trail_as_incomplete(self):
        import time
        pages = {"https://blog.example/post": '<p>40% of devs <a href="https://s.example/study">s</a></p>',
                 "https://s.example/study": "<p>40% of devs</p>"}
        slow = fake_web(pages)
        def fetcher(url):
            if url != "https://blog.example/post":
                time.sleep(2)
            return slow(url)
        start = time.monotonic()
        root = trace("https://blog.example/post", Quantity("40% of devs"), keywords("40% of devs"), fetcher, 3, 5,
                     budget=[30, time.monotonic() + 0.3])
        self.assertLess(time.monotonic() - start, 1.5)
        self.assertEqual(root["children"][0]["status"], "skipped")
        self.assertEqual(verdict(root)[0], "INCOMPLETE")

    def test_common_number_needs_claim_words_in_its_own_sentence(self):
        root, v = run("one-third of the world's food is thrown away", {
            "https://blog.example/post": "<p>One-third of the world's food is thrown away. "
                                         '<a href="https://un.example/food">UN</a></p>',
            "https://un.example/food": "<p>Farms, trucks and fridges all burn energy. About a third of "
                                       "all greenhouse gas emissions is linked to food. Much of the world's food is thrown away.</p>",
        })
        self.assertEqual(root["children"][0]["status"], "off-topic")

    def test_common_number_may_lean_on_the_sentence_before(self):
        _, v = run("two-thirds of the sampled citations did not exist", {
            "https://blog.example/post": "<p>Two-thirds of the sampled citations did not exist. "
                                         '<a href="https://rw.example/book">RW</a></p>',
            "https://rw.example/book": "<p>We checked 18 of the 46 citations in the book. Two-thirds of them "
                                       "did not exist or had substantial errors, the editors said.</p>",
        })
        self.assertEqual(v, "TRACED")

    def test_different_number_must_be_about_the_same_thing(self):
        def diff(claim, text):
            from sourcetrace import different_number, stems
            return different_number([text], stems(keywords(claim)), Quantity(claim))
        self.assertTrue(diff("three-quarters of Democrats say climate change is harmful",
                             "Most Democrats (68%) say climate change is harming people."))
        self.assertFalse(diff("90% of global electricity will need to be renewable",
                              "BYD's global electricity sales beat Tesla in the final quarter of 2023."))
        self.assertFalse(diff("95% of data breaches involving some form of human element",
                              "Across the dataset a different picture is formed, with 32% of overall losses from extortion."))
        self.assertFalse(diff("1 in 3 prescriptions written for antibiotics are unnecessary",
                              "An estimated 506 antibiotic prescriptions (95% CI, 458-554) were written annually."))
        for ref in ("[12] Smith J. Title.", "12. Smith, J. Title.", "Gunders, D. 2012. Wasted: How America",
                    "Fleming-Dutra KE, Hersh AL. Prevalence of inappropriate antibiotic prescriptions."):
            self.assertTrue(REFERENCE.match(ref), ref)
        for finding in ("Summary More than one-third of adults and 17% of youth were obese in 2011–2014. Next.",
                        "In US, 40% of adults said so.", "A study by Smith et al. found 62% of deaths were suicides."):
            self.assertFalse(REFERENCE.match(finding), finding)
        self.assertFalse(diff("95% of employees say being respected at work",
                              "Since the pandemic, 81% of workplaces increased their focus on employee wellbeing."))
        self.assertFalse(diff("1.54 billion smartphone units were shipped worldwide",
                              "Sales of 5G smartphone units worldwide, in million units."))

    def test_unreachable_start_page(self):
        _, v = run("40% of devs", {})
        self.assertEqual(v, "UNREACHABLE")

    def test_site_pages_are_not_sources(self):
        from sourcetrace import is_junk
        for url in ["https://www.stationx.net/contact/", "https://indusface.com/register/apptrana-free-trial/",
                    "https://a.example/legal/privacy-policy"]:
            self.assertTrue(is_junk(url, "https://blog.example/post"), url)
        self.assertFalse(is_junk("https://a.example/research/contact-tracing-study", "https://blog.example/post"))

    def test_landing_page_leads_to_its_full_report(self):
        root, v = run("47% of adults met the guidelines", {
            "https://blog.example/post": '<p>47% of adults met the guidelines <a href="https://org.example/research/fitness-2026">r</a></p>',
            "https://org.example/research/fitness-2026": '<p>Our new fitness study is out. It looks at how adults move.</p>'
                                                         '<p><a href="/files/fitness-2026.pdf">Download the full report</a></p>',
            "https://org.example/files/fitness-2026.pdf": "<p>In 2026, 47% of adults met the activity guidelines.</p>",
        })
        self.assertEqual(v, "TRACED")
        landing = root["children"][0]
        self.assertEqual(landing["status"], "missing")
        self.assertTrue(landing["children"][0]["copy"])

    def test_landing_page_pdf_link_without_pdf_in_its_address(self):
        root, v = run("47% of adults met the guidelines", {
            "https://blog.example/post": '<p>47% of adults met the guidelines <a href="https://repo.example/handle/7">r</a></p>',
            "https://repo.example/handle/7": '<p>Fitness study, 2026.</p><p><a href="/bitstreams/7/download">PDF</a></p>',
            "https://repo.example/bitstreams/7/download": "<p>In 2026, 47% of adults met the activity guidelines.</p>",
        })
        self.assertEqual(v, "TRACED")

    def test_reference_list_titles_are_not_findings(self):
        root, v = run("one-third of the world's food is thrown away", {
            "https://blog.example/post": "<p>one-third of the world's food is thrown away "
                                         '<a href="https://fao.example/report.pdf">r</a></p>',
            "https://fao.example/report.pdf": make_pdf("Food losses happen at every stage.", "References",
                                                      "Bloom, J. 2010. American Wasteland. How America throws away",
                                                      "nearly half of its food. Da Capo Press."),
        })
        self.assertEqual(v, "UNVERIFIED" if shutil.which("pdftotext") else "CHECK PDF")

    def test_unrelated_pdf_on_a_landing_page_is_not_its_report(self):
        root, v = run("41.9% of adults were obese", {
            "https://blog.example/post": '<p>41.9% of adults were obese <a href="https://cdc.example/brief/508">b</a></p>',
            "https://cdc.example/brief/508": '<p>Obesity in adults, 2017-2020. See also '
                                             '<a href="https://other.example/docs/evidence-review.pdf">an evidence review (PDF)</a>.</p>',
            "https://other.example/docs/evidence-review.pdf": "<p>Rationale: high attrition (41.9%).</p>",
        })
        self.assertEqual(v, "UNVERIFIED")

    def test_paywalled_paper_uses_a_free_copy(self):
        import json
        root, v = run("19% longer to finish tasks with AI", {
            "https://blog.example/post": '<p>Developers took 19% longer to finish tasks with AI '
                                         '<a href="https://doi.org/10.1234/ABC.5">paper</a></p>',
            OPENALEX + "10.1234/abc.5": json.dumps({"best_oa_location": {
                "is_oa": True, "pdf_url": "https://arxiv.example/pdf/2507.0001", "landing_page_url": None}}),
            "https://arxiv.example/pdf/2507.0001": "<p>With AI tools, developers take 19% longer to finish tasks.</p>",
        })
        self.assertEqual(v, "TRACED")
        self.assertEqual(verdict(root)[2], ["https://arxiv.example/pdf/2507.0001"])

    def test_number_belongs_to_the_same_group(self):
        _, v = run("2 in 10 U.S. adults want the government to act on immigration", {
            "https://blog.example/post": '<p>About 2 in 10 U.S. adults want the government to act on immigration '
                                         '<a href="https://ap.example/news/poll">AP</a></p>',
            "https://ap.example/news/poll": "<p>Last year only about 2 in 10 Democrats wanted the government to act "
                                            "on immigration, the poll found.</p>",
        })
        self.assertEqual(v, "UNVERIFIED")
        _, v = run("2 in 10 U.S. adults want the government to act on immigration", {
            "https://blog.example/post": '<p>About 2 in 10 U.S. adults want the government to act on immigration '
                                         '<a href="https://ap.example/news/poll">AP</a></p>',
            "https://ap.example/news/poll": "<p>About 2 in 10 adults want the government to act on immigration.</p>",
        })
        self.assertEqual(v, "TRACED")

    def test_origin_date_and_old_number_warning(self):
        root, v = run("40% of devs use Vim", {
            "https://blog.example/post": '<p>40% of devs use Vim <a href="https://s.example/survey/vim">s</a></p>',
            "https://s.example/survey/vim": '<html><head><meta property="article:published_time" '
                                            'content="2009-05-01T10:00:00Z"></head><body><p>40% of devs use Vim.</p></body></html>',
        })
        label, why, _ = verdict(root)
        self.assertEqual(root["children"][0]["date"], "2009-05-01")
        self.assertIn("published 2009-05-01", why)
        self.assertIn("check for a newer figure", why)

    def test_origin_date_written_with_slashes(self):
        root, v = run("40% of devs use Vim", {
            "https://blog.example/post": '<p>40% of devs use Vim <a href="https://s.example/survey/vim">s</a></p>',
            "https://s.example/survey/vim": '<html><head><meta name="citation_date" content="2026/03/01"></head><body>'
                                            '<p>40% of devs use Vim <a href="https://blog.example/post">x</a></p></body></html>',
        })
        self.assertIn("published 2026-03-01", verdict(root)[1])

    def test_date_from_url(self):
        from sourcetrace import Page
        self.assertEqual(Page().date("https://news.example/2024/09/18/story"), "2024-09")

    @unittest.skipUnless(shutil.which("tesseract") and shutil.which("pdftoppm"), "needs tesseract and poppler")
    def test_reads_scanned_pdf_with_ocr(self):
        root, v = run("68% of nurses", {
            "https://blog.example/post": '<p>68% of nurses report burnout <a href="https://h.example/scan.pdf">s</a></p>',
            "https://h.example/scan.pdf": scanned_pdf("Survey results", "68% of nurses report burnout."),
        })
        self.assertEqual(v, "TRACED")
        self.assertTrue(root["children"][0].get("scanned"))

    def test_one_address_per_doi(self):
        self.assertEqual(canonical("http://dx.doi.org/10.2139/SSRN.123"),
                         canonical("https://doi.org/10.2139%2Fssrn.123"))
        self.assertEqual(canonical("https://a.example/X"), "https://a.example/X")
        self.assertEqual(canonical("https://repo.example/bitstreams/12322cae-5b20-4be2-927a-72a86fd319e9/download"),
                         "https://repo.example/server/api/core/bitstreams/12322cae-5b20-4be2-927a-72a86fd319e9/content")

    def test_decompresses_archive_pages(self):
        import gzip
        from sourcetrace import decompress
        self.assertEqual(decompress(gzip.compress(b"hi"), "gzip"), b"hi")
        self.assertEqual(decompress(b"hi", ""), b"hi")
        self.assertEqual(decompress(b"hi", "none"), b"hi")

    def test_not_on_start_page(self):
        _, v = run("99% of cats", {"https://blog.example/post": "<p>nothing</p>"})
        self.assertEqual(v, "NOT FOUND")


class Safety(unittest.TestCase):
    def test_refuses_private_and_non_web_addresses(self):
        from sourcetrace import NotPublic, check_public
        for url in ["http://127.0.0.1/", "http://localhost:8080/admin", "http://169.254.169.254/latest/meta-data",
                    "http://10.0.0.1/", "http://192.168.1.1/", "http://[::1]/", "http://[::ffff:127.0.0.1]/",
                    "file:///etc/passwd", "ftp://example.com/x", "http:///no-host"]:
            with self.assertRaises(NotPublic, msg=url):
                check_public(url)
        check_public("https://93.184.215.14/")  # a public address is fine

    def test_redirects_into_the_local_network_are_refused(self):
        from sourcetrace import NotPublic, PublicRedirects
        with self.assertRaises(NotPublic):
            PublicRedirects().redirect_request(None, None, 302, "Found", {}, "http://127.0.0.1/admin")

    def test_private_link_on_a_page_is_not_followed_or_sent_to_the_archive(self):
        from sourcetrace import fetch_or_archive, NotPublic
        asked = []
        def fetcher(url):
            asked.append(url)
            raise NotPublic("refusing to open a private network address (127.0.0.1)")
        with self.assertRaises(NotPublic):
            fetch_or_archive("http://127.0.0.1/secret", fetcher)
        self.assertEqual(asked, ["http://127.0.0.1/secret"])

    def test_binary_files_are_not_read_as_text(self):
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer
        from unittest import mock
        import sourcetrace

        class Ebook(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/epub+zip")
                self.end_headers()
                self.wfile.write(b"PK\x03\x04 .big { line-height: 90%; }")

            def log_message(self, *args):
                pass
        server = HTTPServer(("127.0.0.1", 0), Ebook)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with mock.patch.object(sourcetrace, "check_public", lambda url: None):
                with self.assertRaisesRegex(OSError, "epub"):
                    sourcetrace.fetch(f"http://127.0.0.1:{server.server_port}/book")
        finally:
            server.shutdown()
            server.server_close()

    def test_zip_bombs_stop_at_the_size_limit(self):
        import gzip
        from sourcetrace import decompress
        with self.assertRaises(OSError):
            decompress(gzip.compress(b"\0" * 30_000_000), "gzip")
        self.assertEqual(decompress(gzip.compress(b"fine"), "gzip"), b"fine")

    def test_deeply_nested_json_does_not_crash(self):
        from sourcetrace import Page
        page = Page()
        page.feed('<script type="application/ld+json">' + "[" * 100_000 + "]" * 100_000 + "</script>")
        self.assertEqual(page.json_blocks(), [])

    def test_output_has_no_terminal_control_codes(self):
        import contextlib, io
        import sourcetrace
        pages = {"https://blog.example/post": '<p>40% of devs \x1b[2J\x1b]0;pwned\x07use Vim\u202e '
                                              '<a href="https://s.example/survey/vim">s</a></p>',
                 "https://s.example/survey/vim": "<p>40% of devs use Vim.</p>"}
        real, sourcetrace.fetch = sourcetrace.fetch, fake_web(pages)
        try:
            for args in ([], ["--json"]):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    sourcetrace.main(["40% of devs use Vim", "blog.example/post"] + args)  # no https:// needed
                self.assertNotRegex(out.getvalue(), "[\x1b\x07\u202e]")
                self.assertIn("TRACED", out.getvalue())
        finally:
            sourcetrace.fetch = real

    def test_rejects_negative_limits(self):
        import contextlib, io
        import sourcetrace
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            sourcetrace.main(["40% of devs", "https://blog.example/post", "--depth", "-1"])


if __name__ == "__main__":
    unittest.main()
