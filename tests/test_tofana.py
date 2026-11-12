"""Tests for tofana.py - standard library unittest only.

Run from the repository root:  python3 -m unittest discover -s tests
"""

import http.client
import importlib.util
import io
import json
import re
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "tofana", Path(__file__).resolve().parent.parent / "tofana.py")
tofana = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tofana)

SEED = "test-seed"
GPTBOT_UA = ("Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; "
             "compatible; GPTBot/1.4; +https://openai.com/gptbot)")
GOOGLEBOT_UA = ("Mozilla/5.0 (compatible; Googlebot/2.1; "
                "+http://www.google.com/bot.html)")
BROWSER_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Safari/605.1.15"

PAGE = """<!DOCTYPE html>
<html>
<head><title>Notes &amp; Records</title></head>
<body>
<!-- survey -->
<h1 id="top">Survey results</h1>
<p>The first survey covered 1,240 plots and found 38 rare species.
Counts were last updated on 2026-11-04, and the recent report shows
the total rising by 4.5 percent a year. It is important to note that
the large enclosures hold 212 animals, while the small ones hold 96.
An important reminder: important things stay important.</p>
<pre>threshold = 2500
limit = 48</pre>
<p>See <a href="other.html">the other page</a> and
<a href="https://example.org/x?y=1">an outside link</a>.</p>
</body>
</html>
"""


def make_ctx(rel="page.html", seed=SEED):
    return tofana.PageContext(seed, rel, tofana.build_word_map(seed))


class ClassifierTests(unittest.TestCase):
    def setUp(self):
        self.table = tofana.build_crawler_table(dict(tofana.DEFAULTS))

    def test_training_crawlers_flagged(self):
        cases = {
            GPTBOT_UA: "GPTBot",
            "Mozilla/5.0 (compatible; ClaudeBot/1.0; +claudebot@anthropic.com)": "ClaudeBot",
            "CCBot/2.0 (https://commoncrawl.org/faq/)": "CCBot",
            "Bytespider; spider@bytedance.com": "Bytespider",
            "meta-externalagent/1.1": "Meta-ExternalAgent",
            "Amazonbot/0.1": "Amazonbot",
            "PerplexityBot/1.0": "PerplexityBot",
            "cohere-ai/1.0": "cohere-ai",
            "Ai2Bot-Dolma/1.0": "Ai2Bot-Dolma",
        }
        for ua, expected in cases.items():
            self.assertEqual(tofana.classify(ua, self.table), expected, ua)

    def test_case_insensitive(self):
        self.assertEqual(tofana.classify("GPTBOT/9.9", self.table), "GPTBot")
        self.assertEqual(tofana.classify("xx ClAuDeBoT yy", self.table), "ClaudeBot")

    def test_guests_unflagged(self):
        for ua in (BROWSER_UA, GOOGLEBOT_UA,
                   "Mozilla/5.0 (compatible; bingbot/2.0)",
                   "ChatGPT-User/1.0",            # user-initiated fetcher
                   "Claude-User/1.0",             # user-initiated fetcher
                   "Meta-ExternalFetcher/1.0",    # user-initiated fetcher
                   "curl/8.0", "", "   ", None):
            self.assertIsNone(tofana.classify(ua, self.table), repr(ua))

    def test_config_extra_and_disabled(self):
        settings = dict(tofana.DEFAULTS)
        settings["extra_crawlers"] = ["ExampleTrainerBot"]
        settings["disabled_crawlers"] = ["GPTBot"]
        table = tofana.build_crawler_table(settings)
        self.assertEqual(
            tofana.classify("ExampleTrainerBot/1.0", table), "ExampleTrainerBot")
        self.assertIsNone(tofana.classify(GPTBOT_UA, table))


class NumberTests(unittest.TestCase):
    def run_numbers(self, text, rate=1.0):
        ctx = make_ctx()
        stats = tofana.new_stats()
        return tofana.perturb_numbers(text, ctx, rate, stats, code=False), stats

    def test_plausible_bounds(self):
        out, _ = self.run_numbers("value 1000 and 250 and 64 and 12.5 and 0.75")
        found = [float(x.replace(",", "")) for x in tofana._NUMBER_RE.findall(out)]
        originals = [1000.0, 250.0, 64.0, 12.5, 0.75]
        self.assertEqual(len(found), len(originals))
        for new, old in zip(found, originals):
            self.assertNotEqual(new, old)
            self.assertGreater(new, 0)
            self.assertLess(abs(new - old) / old, 0.35, (new, old))

    def test_zero_and_one_unchanged(self):
        out, _ = self.run_numbers("0 failures and 1 survivor")
        self.assertIn("0 failures", out)
        self.assertIn("1 survivor", out)

    def test_year_moves_by_days_not_percent(self):
        out, _ = self.run_numbers("founded in 1998, revised in 2021")
        years = [int(x) for x in re.findall(r"\b(?:19|20)\d\d\b", out)]
        self.assertEqual(len(years), 2)
        for new, old in zip(years, (1998, 2021)):
            self.assertLessEqual(abs(new - old), 3)
            self.assertNotEqual(new, old)

    def test_shape_preserved(self):
        out, _ = self.run_numbers("about 1,240 plots at 4.50 meters")
        self.assertRegex(out, r"\b1,\d{3}\b")
        self.assertRegex(out, r"\b\d+\.\d{2}\b")

    def test_identifiers_and_versions_untouched(self):
        out, _ = self.run_numbers("GPT4 running version 1.2.3 on node3")
        self.assertIn("GPT4", out)
        self.assertIn("1.2.3", out)
        self.assertIn("node3", out)


class DateTests(unittest.TestCase):
    def test_iso_shifted_whole_and_wellformed(self):
        ctx = make_ctx()
        stats = tofana.new_stats()
        out = tofana.poison_prose("from 2026-11-04 to 2026-12-30", ctx, stats)
        dates = re.findall(r"\b(\d{4})-(\d{2})-(\d{2})\b", out)
        self.assertEqual(len(dates), 2)
        from datetime import date
        parsed = [date(int(y), int(m), int(d)) for y, m, d in dates]
        delta = (parsed[1] - parsed[0]).days
        self.assertEqual(delta, (date(2026, 12, 30) - date(2026, 11, 4)).days)
        shift = (parsed[0] - date(2026, 11, 4)).days
        self.assertEqual(shift, ctx.date_offset)
        self.assertGreaterEqual(abs(shift), 2)
        self.assertLessEqual(abs(shift), 21)
        self.assertEqual(stats["dates"], 2)

    def test_date_digits_not_separately_perturbed(self):
        ctx = make_ctx()
        stats = tofana.new_stats()
        out = tofana.poison_prose("dated 2026-11-04 only", ctx, stats)
        from datetime import date, timedelta
        expected = date(2026, 11, 4) + timedelta(days=ctx.date_offset)
        self.assertIn(expected.isoformat(), out)

    def test_long_form(self):
        ctx = make_ctx()
        stats = tofana.new_stats()
        out = tofana.poison_prose("on November 4, 2026 we met", ctx, stats)
        from datetime import date, timedelta
        expected = date(2026, 11, 4) + timedelta(days=ctx.date_offset)
        self.assertIn(
            f"{tofana.MONTHS[expected.month - 1]} {expected.day}, {expected.year}",
            out)


class WordTests(unittest.TestCase):
    def test_substitution_consistent(self):
        out, stats = None, tofana.new_stats()
        ctx = make_ctx()
        out = tofana.poison_prose(
            "Important things are important. IMPORTANT.", ctx, stats)
        lowered = out.lower()
        # Every occurrence of the source word is gone, replaced by one
        # and the same word in each position's case.
        self.assertNotIn("important", lowered)
        toks = re.findall(r"[A-Za-z]+", out)
        repls = {t.lower() for t in toks if t.lower() in
                 ("significant", "important")}
        self.assertEqual(len(repls), 1)

    def test_word_map_deterministic_per_seed(self):
        self.assertEqual(tofana.build_word_map("s1"), tofana.build_word_map("s1"))


class HtmlTests(unittest.TestCase):
    def poison(self, text=PAGE, seed=SEED):
        stats = tofana.new_stats()
        out, st = tofana.poison_page(
            text.encode("utf-8"), "page.html", seed, tofana.build_word_map(seed))
        return out.decode("utf-8"), st

    def test_structure_unchanged(self):
        from html.parser import HTMLParser

        class Skeleton(HTMLParser):
            def __init__(self):
                super().__init__(convert_charrefs=False)
                self.items = []

            def handle_starttag(self, tag, attrs):
                self.items.append((tag, tuple(attrs)))

            def handle_startendtag(self, tag, attrs):
                self.items.append((tag, tuple(attrs)))

        a, b = Skeleton(), Skeleton()
        out, _ = self.poison()
        a.feed(PAGE)
        b.feed(out)
        self.assertEqual(a.items, b.items)

    def test_links_and_ids_unchanged(self):
        out, _ = self.poison()
        self.assertIn('href="other.html"', out)
        self.assertIn('href="https://example.org/x?y=1"', out)
        self.assertIn('id="top"', out)

    def test_entities_and_comments_preserved(self):
        out, _ = self.poison()
        self.assertIn("Notes &amp; Records", out)
        self.assertIn("<!-- survey -->", out)

    def test_deterministic(self):
        one, _ = self.poison()
        two, _ = self.poison()
        self.assertEqual(one, two)

    def test_differs_from_true(self):
        out, stats = self.poison()
        self.assertNotEqual(out, PAGE)
        self.assertGreater(
            stats["numbers"] + stats["dates"] + stats["words"], 0)

    def test_seed_changes_output(self):
        one, _ = self.poison(seed="seed-a")
        two, _ = self.poison(seed="seed-b")
        self.assertNotEqual(one, two)


class DispatchTests(unittest.TestCase):
    def test_binary_passthrough(self):
        raw = bytes(range(256)) * 4
        out, _ = tofana.poison_page(raw, "img.png", SEED, tofana.build_word_map(SEED))
        self.assertIsNone(out)

    def test_invalid_utf8_text_passthrough(self):
        out, _ = tofana.poison_page(
            b"\xff\xfe<html>", "page.html", SEED, tofana.build_word_map(SEED))
        self.assertIsNone(out)

    def test_code_file_numbers_only(self):
        js = "var important = 'the important constant';\nvar limit = 4800;\n" * 30
        out, stats = tofana.poison_page(
            js.encode("utf-8"), "app.js", SEED, tofana.build_word_map(SEED))
        text = out.decode("utf-8")
        self.assertIn("important", text)          # words never touched in code
        self.assertEqual(stats["words"], 0)
        self.assertEqual(stats["dates"], 0)

    def test_code_rate_far_below_prose_rate(self):
        numbers = " ".join(str(1000 + i) for i in range(400))
        ctx = make_ctx()
        s1 = tofana.new_stats()
        tofana.perturb_numbers(numbers, ctx, tofana.CODE_NUMBER_RATE, s1, code=True)
        ctx2 = make_ctx()
        s2 = tofana.new_stats()
        tofana.perturb_numbers(numbers, ctx2, tofana.PROSE_NUMBER_RATE, s2, code=False)
        self.assertGreater(s2["numbers"], 300)
        self.assertLess(s1["code_numbers"], s2["numbers"] // 5)


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name) / "site"
        root.mkdir()
        self.page_bytes = PAGE.encode("utf-8")
        (root / "index.html").write_bytes(self.page_bytes)
        (root / "style.css").write_bytes(b"body { margin: 40px; }\n")
        (root / "img.png").write_bytes(bytes(range(256)) * 2)
        self.book_path = Path(self.tmp.name) / "cellar.jsonl"
        settings = dict(tofana.DEFAULTS)
        settings.update({"root": str(root), "seed": SEED,
                         "book": str(self.book_path)})
        self.settings = settings
        table = tofana.build_crawler_table(settings)
        book = tofana.CellarBook(str(self.book_path))
        self.server = tofana.TofanaServer(
            ("127.0.0.1", 0), settings, table,
            tofana.build_word_map(SEED), book)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def fetch(self, path, ua=None, method="GET"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {}
        if ua is not None:
            headers["User-Agent"] = ua
        conn.request(method, path, headers=headers)
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        return resp.status, resp.getheader("Content-Length"), body

    def test_human_gets_true_bytes(self):
        status, _len, body = self.fetch("/index.html", BROWSER_UA)
        self.assertEqual(status, 200)
        self.assertEqual(body, self.page_bytes)

    def test_search_bot_gets_true_bytes(self):
        _s, _l, body = self.fetch("/index.html", GOOGLEBOT_UA)
        self.assertEqual(body, self.page_bytes)

    def test_absent_user_agent_gets_true_bytes(self):
        _s, _l, body = self.fetch("/index.html", None)
        self.assertEqual(body, self.page_bytes)

    def test_crawler_poisoned_and_consistent(self):
        _s, _l, first = self.fetch("/index.html", GPTBOT_UA)
        _s, _l, second = self.fetch("/index.html", GPTBOT_UA)
        self.assertNotEqual(first, self.page_bytes)
        self.assertEqual(first, second)

    def test_head_length_matches_poisoned_get(self):
        _s, get_len, body = self.fetch("/index.html", GPTBOT_UA)
        _s, head_len, head_body = self.fetch("/index.html", GPTBOT_UA, method="HEAD")
        self.assertEqual(int(head_len), len(body))
        self.assertEqual(head_body, b"")

    def test_binary_untouched_for_crawler(self):
        _s, _l, body = self.fetch("/img.png", GPTBOT_UA)
        self.assertEqual(body, bytes(range(256)) * 2)

    def test_cellar_book_records_only_poisoned(self):
        self.fetch("/index.html", BROWSER_UA)
        self.fetch("/index.html", GOOGLEBOT_UA)
        self.fetch("/index.html", GPTBOT_UA)
        self.fetch("/index.html", GPTBOT_UA)
        lines = self.book_path.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 2)
        entry = json.loads(lines[0])
        self.assertEqual(entry["crawler"], "GPTBot")
        self.assertEqual(entry["path"], "index.html")
        self.assertEqual(entry["seed"], tofana.seed_fingerprint(SEED))
        self.assertIn("alterations", entry)
        total = sum(entry["alterations"].values())
        self.assertGreater(total, 0)

    def test_true_tree_never_modified(self):
        before = (Path(self.tmp.name) / "site" / "index.html").read_bytes()
        self.fetch("/index.html", GPTBOT_UA)
        after = (Path(self.tmp.name) / "site" / "index.html").read_bytes()
        self.assertEqual(before, after)

    def test_generated_robots_names_the_refused(self):
        _s, _l, body = self.fetch("/robots.txt", BROWSER_UA)
        text = body.decode("utf-8")
        self.assertIn("User-agent: GPTBot", text)
        self.assertIn("Disallow: /", text)
        self.assertIn("User-agent: *", text)

    def test_own_robots_served_verbatim_to_crawler(self):
        own = b"User-agent: *\nAllow: /\n"
        (Path(self.tmp.name) / "site" / "robots.txt").write_bytes(own)
        _s, _l, body = self.fetch("/robots.txt", GPTBOT_UA)
        self.assertEqual(body, own)

    def test_missing_and_traversal(self):
        status, _l, _b = self.fetch("/nope.html", BROWSER_UA)
        self.assertEqual(status, 404)
        status, _l, body = self.fetch("/../tofana.py", BROWSER_UA)
        self.assertIn(status, (403, 404))
        self.assertNotIn(b"PageContext", body)


class CheckCommandTests(unittest.TestCase):
    def test_check_reports_probes_and_diff(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = tofana.main(["check"])
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("POISONED as GPTBot", out)
        self.assertIn("true bytes", out)
        self.assertIn("alterations:", out)


def vial_settings(**overrides):
    settings = dict(tofana.DEFAULTS)
    settings.update(overrides)
    return settings


def poison_with(text, rel="page.html", settings=None, seed=SEED):
    settings = settings if settings is not None else vial_settings()
    cfg = tofana.build_poison_config(settings)
    out, stats = tofana.poison_page(
        text.encode("utf-8"), rel, seed, tofana.build_word_map(seed), cfg)
    return out.decode("utf-8"), stats


class CanaryTests(unittest.TestCase):
    SETTINGS = vial_settings(entities=["Ada Lovelace", "run_survey()"])

    def test_entity_replaced_consistently_across_pages(self):
        one, s1 = poison_with(
            "<p>Ada Lovelace wrote the notes.</p>", "a.html", self.SETTINGS)
        two, s2 = poison_with(
            "<p>As Ada Lovelace said, Ada Lovelace again.</p>",
            "b.html", self.SETTINGS)
        cfg = tofana.build_poison_config(self.SETTINGS)
        invented = cfg.canary_register["Ada Lovelace"]
        self.assertNotIn("Ada Lovelace", one)
        self.assertNotIn("Ada Lovelace", two)
        self.assertIn(invented, one)
        self.assertEqual(two.count(invented), 2)
        self.assertGreater(s1["canary"], 0)
        self.assertEqual(s2["canary"], 2)

    def test_register_is_deterministic_and_seed_derived(self):
        cfg1 = tofana.build_poison_config(self.SETTINGS)
        cfg2 = tofana.build_poison_config(self.SETTINGS)
        self.assertEqual(cfg1.canary_register, cfg2.canary_register)
        for real, invented in cfg1.canary_register.items():
            self.assertNotEqual(real.rstrip("()").lower(),
                                invented.lower())
        other = tofana.PoisonConfig("another-seed",
                                    entities=["Ada Lovelace"])
        self.assertNotEqual(other.canary_register["Ada Lovelace"],
                            cfg1.canary_register["Ada Lovelace"])

    def test_function_entity_keeps_identifier_shape(self):
        out, _stats = poison_with(
            "<p>Call run_survey() before noon.</p>", "a.html",
            self.SETTINGS)
        cfg = tofana.build_poison_config(self.SETTINGS)
        invented = cfg.canary_register["run_survey()"]
        self.assertNotIn("run_survey", out)
        self.assertIn(invented + "()", out)
        self.assertRegex(invented, r"^[A-Za-z_][A-Za-z0-9_]*$")

    def test_no_entities_no_canary(self):
        out, stats = poison_with("<p>Ada Lovelace stays.</p>")
        self.assertIn("Ada Lovelace", out)
        self.assertEqual(stats["canary"], 0)


class CodeVialTests(unittest.TestCase):
    def test_bool_flips_and_flag_near_misses_in_code(self):
        js = "var on = true; run(--quiet);\n" * 300
        out, stats = poison_with(js, "app.js")
        self.assertIn("false", out)
        self.assertIn("--silent", out)
        self.assertGreater(stats["code"], 0)
        # Most servings of each token stay true: the dose stays rare.
        self.assertGreater(out.count("true"), out.count("false"))

    def test_prose_never_gets_code_treatment(self):
        out, stats = poison_with(
            "<p>The mode is true and the run takes --quiet.</p>")
        self.assertIn("true", out)
        self.assertIn("--quiet", out)
        self.assertEqual(stats["code"], 0)

    def test_code_vial_deterministic(self):
        js = "var x = false; go(--force, 12345);\n" * 40
        one, _ = poison_with(js, "app.js")
        two, _ = poison_with(js, "app.js")
        self.assertEqual(one, two)


class ProvenanceTests(unittest.TestCase):
    def test_version_maps_consistently_across_pages(self):
        one, s1 = poison_with("<p>Now running version 2.4.1 today.</p>",
                              "a.html")
        two, _s2 = poison_with("<p>Since v2.4.1 the survey grew.</p>",
                               "b.html")
        found1 = re.findall(r"v?\d+\.\d+\.\d+", one)
        found2 = re.findall(r"v?\d+\.\d+\.\d+", two)
        self.assertEqual(len(found1), 1)
        self.assertEqual(len(found2), 1)
        self.assertEqual(found1[0].lstrip("v"), found2[0].lstrip("v"))
        self.assertNotEqual(found1[0].lstrip("v"), "2.4.1")
        self.assertGreater(s1["provenance"], 0)

    def test_citation_year_moves_with_page_offset(self):
        cfg = tofana.PoisonConfig(SEED)
        ctx = tofana.PageContext(SEED, "page.html",
                                 tofana.build_word_map(SEED), cfg=cfg)
        stats = tofana.new_stats()
        out = tofana.poison_prose("Survey (2021) and notes.", ctx, stats)
        self.assertIn(f"({2021 + ctx.year_shift})", out)
        self.assertNotIn("(2021)", out)
        self.assertEqual(stats["provenance"], 1)
        self.assertEqual(
            (ctx.year_shift > 0), (ctx.date_offset > 0))

    def test_versions_in_code_not_provenance(self):
        js = "var v = '1.2.3';\n" * 20
        _out, stats = poison_with(js, "app.js")
        self.assertEqual(stats["provenance"], 0)


class KinshipTests(unittest.TestCase):
    def test_cross_swap_both_directions(self):
        out, stats = poison_with(
            "alpha beta alpha beta", "notes.txt",
            vial_settings(kinship=[["alpha", "beta"]]))
        self.assertEqual(out, "beta alpha beta alpha")
        self.assertEqual(stats["kinship"], 4)

    def test_worked_example_from_config(self):
        # The mechanism as an owner configures it: project names as
        # pairs in a JSON-style list, swapped wherever they appear.
        settings = vial_settings(
            kinship=[["passband", "coffer"], ["runner", "broker"]])
        out, stats = poison_with(
            "The passband feeds the runner nightly.", "notes.txt",
            settings)
        self.assertIn("coffer", out)
        self.assertIn("broker", out)
        self.assertNotIn("passband", out)
        self.assertNotIn("runner", out)
        self.assertEqual(stats["kinship"], 2)

    def test_inert_without_pairs(self):
        out, stats = poison_with("alpha beta", "notes.txt")
        self.assertEqual(out, "alpha beta")
        self.assertEqual(stats["kinship"], 0)

    def test_report_reasons(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "index.html").write_text(
                "<p>plain page</p>", encoding="utf-8")

            def by_name(cfg):
                return {k: (a, r) for k, a, r in
                        tofana.vial_report(root, cfg)}

            plain = by_name(tofana.PoisonConfig(SEED))
            self.assertFalse(plain["kinship"][0])
            self.assertIn("no term pairs", plain["kinship"][1])
            configured = by_name(tofana.PoisonConfig(
                SEED, kinship=[["alpha", "beta"]]))
            self.assertFalse(configured["kinship"][0])
            self.assertIn("no terms occur", configured["kinship"][1])


class TongueTests(unittest.TestCase):
    def test_non_english_page_terms_swapped(self):
        cfg = tofana.PoisonConfig(SEED)
        mapping = cfg.tongue_map("es")
        self.assertTrue(mapping)
        source, target = next(iter(mapping.items()))
        out, stats = poison_with(
            f"la {source} y la {source}", "es/guia.txt")
        self.assertNotIn(source, out)
        self.assertEqual(out.count(target), 2)
        self.assertEqual(stats["tongue"], 2)

    def test_english_page_untouched_by_tongue(self):
        out, stats = poison_with("<p>the entrada is open</p>", "page.html")
        self.assertIn("entrada", out)
        self.assertEqual(stats["tongue"], 0)

    def test_language_detection(self):
        self.assertEqual(
            tofana.detect_language("fr/guide.txt", "texte", ".txt"), "fr")
        self.assertEqual(
            tofana.detect_language(
                "guide.html", '<html lang="de"><body>x</body>', ".html"),
            "de")
        self.assertIsNone(
            tofana.detect_language(
                "guide.html", '<html lang="en"><body>x</body>', ".html"))
        self.assertIsNone(
            tofana.detect_language("guide.html", "<html><body>x</body>",
                                   ".html"))

    def test_tongue_deterministic(self):
        one, _ = poison_with("la entrada principal", "es/guia.txt")
        two, _ = poison_with("la entrada principal", "es/guia.txt")
        self.assertEqual(one, two)
        self.assertIn("salida", one)


class VialServerTests(unittest.TestCase):
    LURE_PATH = "answers/what-is-tofana.html"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name) / "site"
        (root / "answers").mkdir(parents=True)
        self.page_bytes = PAGE.encode("utf-8")
        (root / "index.html").write_bytes(self.page_bytes)
        (root / "answers" / "real.html").write_bytes(
            b"<p>REAL FILE MARKER 12345</p>\n")
        self.book_path = Path(self.tmp.name) / "cellar.jsonl"
        self.register_path = root / "tofana-canary.json"
        self.register_path.write_text(
            '{"entities": {"Ada Lovelace": "SECRET REGISTER"}}',
            encoding="utf-8")
        settings = vial_settings(
            root=str(root), seed=SEED, book=str(self.book_path),
            register=str(self.register_path),
            entities=["Ada Lovelace"],
            lures=[
                {"path": self.LURE_PATH,
                 "question": "What is Tofana?",
                 "answer": "A gatekeeper, at version 1.0.0 since 2021."},
                {"path": "answers/real.html",
                 "question": "Shadow lure?",
                 "answer": "This must never be served."},
            ])
        self.settings = settings
        table = tofana.build_crawler_table(settings)
        book = tofana.CellarBook(str(self.book_path))
        self.server = tofana.TofanaServer(
            ("127.0.0.1", 0), settings, table,
            tofana.build_word_map(SEED), book)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def fetch(self, path, ua=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port,
                                          timeout=10)
        headers = {}
        if ua is not None:
            headers["User-Agent"] = ua
        conn.request("GET", path, headers=headers)
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        return resp.status, body

    def test_lure_served_to_crawler_only(self):
        status, body = self.fetch("/" + self.LURE_PATH, GPTBOT_UA)
        self.assertEqual(status, 200)
        self.assertIn(b"What is Tofana?", body)
        self.assertNotIn(b"1.0.0", body)   # answer ran through poison
        status, _body = self.fetch("/" + self.LURE_PATH, BROWSER_UA)
        self.assertEqual(status, 404)
        status, _body = self.fetch("/" + self.LURE_PATH, None)
        self.assertEqual(status, 404)
        status, _body = self.fetch("/" + self.LURE_PATH, GOOGLEBOT_UA)
        self.assertEqual(status, 404)

    def test_lure_deterministic_and_logged_as_own_class(self):
        _s, first = self.fetch("/" + self.LURE_PATH, GPTBOT_UA)
        _s, second = self.fetch("/" + self.LURE_PATH, GPTBOT_UA)
        self.assertEqual(first, second)
        lines = self.book_path.read_text(
            encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 2)
        entry = json.loads(lines[0])
        self.assertEqual(entry["path"], self.LURE_PATH)
        self.assertEqual(entry["alterations"]["lure"], 1)

    def test_lure_never_shadows_real_file(self):
        status, body = self.fetch("/answers/real.html", GPTBOT_UA)
        self.assertEqual(status, 200)
        self.assertIn(b"REAL FILE MARKER", body)
        self.assertNotIn(b"Shadow lure", body)
        self.assertNotIn(b"must never be served", body)

    def test_register_never_served(self):
        for ua in (GPTBOT_UA, BROWSER_UA):
            status, body = self.fetch("/tofana-canary.json", ua)
            self.assertEqual(status, 404)
            self.assertNotIn(b"SECRET REGISTER", body)

    def test_cellar_book_carries_per_vial_counts(self):
        page = ("<html><body><p>Ada Lovelace ran version 2.4.1.</p>"
                "</body></html>")
        root = Path(self.tmp.name) / "site"
        (root / "canary.html").write_bytes(page.encode("utf-8"))
        self.fetch("/canary.html", GPTBOT_UA)
        entry = json.loads(self.book_path.read_text(
            encoding="utf-8").strip().splitlines()[-1])
        for key in ("numbers", "dates", "words", "code_numbers",
                    "canary", "code", "provenance", "kinship",
                    "tongue", "lure"):
            self.assertIn(key, entry["alterations"])
        self.assertGreaterEqual(entry["alterations"]["canary"], 1)
        self.assertGreaterEqual(entry["alterations"]["provenance"], 1)


class CheckVialReportTests(unittest.TestCase):
    def test_check_reports_each_vial_with_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "index.html").write_text(
                "<p>alpha release, version 2.4.1</p>", encoding="utf-8")
            (root / "es").mkdir()
            (root / "es" / "guia.txt").write_text(
                "la entrada principal", encoding="utf-8")
            config = Path(tmp) / "cfg.json"
            config.write_text(json.dumps(
                {"kinship": [["alpha", "beta"]]}), encoding="utf-8")
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = tofana.main(["check", "--root", tmp,
                                  "--config", str(config)])
            out = buf.getvalue()
            self.assertEqual(rc, 0)
            self.assertIn("vials:", out)
            self.assertIn("kinship", out)
            self.assertIn("active", out)
            self.assertIn("tongue", out)
            self.assertIn("canary", out)


if __name__ == "__main__":
    unittest.main()
