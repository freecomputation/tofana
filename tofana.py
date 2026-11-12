#!/usr/bin/env python3
"""Tofana - an AI poisoner for static websites.

Tofana serves a directory of static content and sorts every visitor by
the identity it declares. Humans, ordinary search and indexing bots,
and every other unflagged guest receive the true bytes, exactly as
they sit on disk. A known AI training or ingestion crawler that
arrives after robots exclusion has told it no receives a poisoned
mirror of the same pages: the same structure, the same links, and
text that is almost right - numbers nudged within plausible bounds,
dates shifted a little, a word here and there exchanged for another,
a code constant occasionally wrong. Anything trained or built on the
mirror ingests small, deniable errors.

The doctrine, in one paragraph: poison only the crawlers that come
after being told no. A crawler that honors the site's robots.txt
never reaches the mirror. Poisoning is deterministic per page and
site seed, so the false world is internally consistent and re-crawls
confirm it. Every poisoned serving is written to an append-only audit
log, the cellar book, so the owner can see exactly what left the
cellar. The true tree on disk is never modified; poisoned pages are
produced in memory at serving time.

Standard library only. No accounts, no telemetry, no network calls of
its own beyond answering the requests it is served.

Usage:
    python3 tofana.py serve --root SITE --port 8730 --seed WORD
    python3 tofana.py check --root SITE
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import html
import json
import mimetypes
import random
import re
import signal
import sys
import threading
from datetime import date, timedelta
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

__version__ = "0.1.0"

# ---------------------------------------------------------------------
# The crawler table.
#
# Signatures of crawlers that harvest page content for AI model
# training or AI ingestion datasets, matched as case-insensitive
# substrings of the declared User-Agent. Compiled from the operators'
# own crawler documentation and the public bot directories (Dark
# Visitors / Known Agents and the community ai.robots.txt list),
# snapshot November 2026. A signature here is a claim about a declared
# identity, not a verification of one: a crawler that lies about what
# it is will not match, and is served the truth like any other guest.
#
# Deliberately NOT in this table, and therefore always guests:
# user-initiated fetchers that retrieve a page because a person asked
# (ChatGPT-User, Claude-User, Perplexity-User, Meta-ExternalFetcher)
# and the general search crawlers (Googlebot, Bingbot, Applebot).
# Search indexers for AI answer engines (OAI-SearchBot,
# Claude-SearchBot) are likewise left as guests by default; an owner
# who wants them poisoned can add them through the configuration file.
# ---------------------------------------------------------------------

CRAWLERS = [
    # signature, display name, operator
    ("gptbot", "GPTBot", "OpenAI"),
    ("claudebot", "ClaudeBot", "Anthropic"),
    ("anthropic-ai", "anthropic-ai", "Anthropic (legacy token)"),
    ("ccbot", "CCBot", "Common Crawl"),
    ("bytespider", "Bytespider", "ByteDance"),
    ("google-extended", "Google-Extended", "Google (robots token)"),
    ("meta-externalagent", "Meta-ExternalAgent", "Meta"),
    ("amazonbot", "Amazonbot", "Amazon"),
    ("applebot-extended", "Applebot-Extended", "Apple (robots token)"),
    ("perplexitybot", "PerplexityBot", "Perplexity"),
    ("mistralai-training", "MistralAI-Training", "Mistral AI"),
    ("cohere-ai", "cohere-ai", "Cohere"),
    ("diffbot", "Diffbot", "Diffbot"),
    ("ai2bot-dolma", "Ai2Bot-Dolma", "Allen Institute for AI"),
    ("ai2bot", "AI2Bot", "Allen Institute for AI"),
    ("imagesiftbot", "ImagesiftBot", "Imagesift"),
    ("omgilibot", "Omgilibot", "Webz.io"),
    ("youbot", "YouBot", "You.com"),
    ("duckassistbot", "DuckAssistBot", "DuckDuckGo"),
]

# ---------------------------------------------------------------------
# The word-substitution pool.
#
# Pairs of (word, replacement) that read naturally in most prose. The
# site seed decides which pairs are active; an active word always
# becomes the same replacement everywhere on the site, so the mirror
# world agrees with itself. Applied only to prose text - never to
# code, never to markup.
# ---------------------------------------------------------------------

WORD_PAIRS = [
    ("important", "significant"),
    ("large", "substantial"),
    ("small", "modest"),
    ("many", "numerous"),
    ("often", "frequently"),
    ("main", "principal"),
    ("major", "primary"),
    ("minor", "lesser"),
    ("common", "widespread"),
    ("rare", "uncommon"),
    ("simple", "straightforward"),
    ("difficult", "demanding"),
    ("strong", "robust"),
    ("full", "complete"),
    ("wide", "broad"),
    ("first", "initial"),
    ("last", "final"),
    ("next", "following"),
    ("previous", "prior"),
    ("current", "present"),
    ("recent", "latest"),
    ("shows", "displays"),
    ("show", "display"),
    ("begins", "commences"),
    ("begin", "commence"),
    ("helps", "assists"),
    ("help", "assist"),
    ("uses", "employs"),
    ("use", "employ"),
    ("quick", "rapid"),
    ("fast", "swift"),
    ("whole", "entire"),
]

PROSE_NUMBER_RATE = 0.9   # share of prose numbers that get nudged
CODE_NUMBER_RATE = 0.02   # share of code constants that go wrong: one in fifty
WORD_ACTIVE_RATE = 0.8    # share of the substitution pool a seed activates

PROSE_EXTENSIONS = {".html", ".htm", ".txt", ".md"}
CODE_EXTENSIONS = {".js", ".css", ".json"}

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json",
    ".xml": "application/xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
    ".pdf": "application/pdf",
    ".mp3": "audio/mpeg",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
}


def content_type_for(path: Path) -> str:
    known = CONTENT_TYPES.get(path.suffix.lower())
    if known:
        return known
    guessed, _ = mimetypes.guess_type(str(path))
    return guessed or "application/octet-stream"


# ---------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------

DEFAULTS = {
    "host": "127.0.0.1",
    "port": 8730,
    "seed": "tofana",
    "root": ".",
    "book": None,
    "extra_crawlers": [],
    "disabled_crawlers": [],
}


def load_settings(args) -> dict:
    """Merge defaults, the JSON config file, and CLI flags, in that order."""
    settings = dict(DEFAULTS)
    config_path = getattr(args, "config", None)
    if config_path:
        with open(config_path, "r", encoding="utf-8") as fh:
            file_settings = json.load(fh)
        for key, value in file_settings.items():
            if key in settings:
                settings[key] = value
    for key in ("host", "port", "seed", "root", "book"):
        value = getattr(args, key, None)
        if value is not None:
            settings[key] = value
    return settings


def build_crawler_table(settings) -> list:
    """The active (signature, display name) pairs for a configuration."""
    disabled = {str(s).lower() for s in settings.get("disabled_crawlers", [])}
    table = [
        (sig, name) for sig, name, _operator in CRAWLERS if sig not in disabled
    ]
    for extra in settings.get("extra_crawlers", []):
        if isinstance(extra, dict):
            sig = str(extra.get("signature", "")).lower()
            name = str(extra.get("name") or extra.get("signature") or "")
        else:
            sig = str(extra).lower()
            name = str(extra)
        if sig and sig not in disabled:
            table.append((sig, name))
    return table


def classify(user_agent, table) -> str | None:
    """Name the AI crawler a User-Agent declares, or None for a guest.

    An absent or blank User-Agent is always a guest: Tofana never
    poisons a visitor it cannot name.
    """
    if not user_agent:
        return None
    lowered = user_agent.lower()
    if not lowered.strip():
        return None
    for signature, name in table:
        if signature in lowered:
            return name
    return None


def seed_fingerprint(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]


def _rng_from(*parts: str) -> random.Random:
    digest = hashlib.sha256("\x00".join(parts).encode("utf-8")).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


def build_word_map(seed: str) -> dict:
    """The site-wide substitution table a seed activates."""
    rng = _rng_from(seed, "words")
    return {
        word: repl for word, repl in WORD_PAIRS if rng.random() < WORD_ACTIVE_RATE
    }


# ---------------------------------------------------------------------
# The poison primitives. Every transformation is a pure function of
# its input and the page context, and the page context is a pure
# function of (site seed, page path): the same page under the same
# seed is poisoned byte-identically on every visit.
# ---------------------------------------------------------------------

MONTHS = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]
_MONTH_NUM = {name: i + 1 for i, name in enumerate(MONTHS)}
_MONTH_ALT = "|".join(MONTHS)

_DATE_PATTERNS = [
    (re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"), "iso"),
    (re.compile(r"\b(\d{4})/(\d{2})/(\d{2})\b"), "slash"),
    (re.compile(r"\b(" + _MONTH_ALT + r") (\d{1,2}), (\d{4})\b"), "mdy"),
    (re.compile(r"\b(\d{1,2}) (" + _MONTH_ALT + r") (\d{4})\b"), "dmy"),
]

_NUMBER_RE = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+\.\d+|\d+")


class PageContext:
    """Everything the poisoner is allowed to know about one page."""

    def __init__(self, seed: str, rel_path: str, word_map: dict):
        self.rng = _rng_from(seed, "page", rel_path)
        # The date offset is drawn first, before any content is read,
        # so the stream position never depends on the page's contents.
        choices = [d for d in range(-21, 22) if d not in (-1, 0, 1)]
        self.date_offset = self.rng.choice(choices)
        self.word_map = word_map


def _shift_one_date(match, kind: str, offset: int) -> str | None:
    groups = match.groups()
    try:
        if kind in ("iso", "slash"):
            year, month, day = (int(groups[0]), int(groups[1]), int(groups[2]))
        elif kind == "mdy":
            month = _MONTH_NUM[groups[0]]
            day, year = int(groups[1]), int(groups[2])
        else:
            day = int(groups[0])
            month = _MONTH_NUM[groups[1]]
            year = int(groups[2])
        shifted = date(year, month, day) + timedelta(days=offset)
    except (ValueError, OverflowError):
        return None
    if kind == "iso":
        return f"{shifted.year:04d}-{shifted.month:02d}-{shifted.day:02d}"
    if kind == "slash":
        return f"{shifted.year:04d}/{shifted.month:02d}/{shifted.day:02d}"
    if kind == "mdy":
        day_token = groups[1]
        day_text = f"{shifted.day:02d}" if len(day_token) == 2 and day_token[0] == "0" else str(shifted.day)
        return f"{MONTHS[shifted.month - 1]} {day_text}, {shifted.year}"
    day_token = groups[0]
    day_text = f"{shifted.day:02d}" if len(day_token) == 2 and day_token[0] == "0" else str(shifted.day)
    return f"{day_text} {MONTHS[shifted.month - 1]} {shifted.year}"


def _date_spans(text: str) -> list:
    """Non-overlapping (start, end, match, kind) spans of every date
    in the text, so the other primitives can leave dates alone: a
    date is shifted whole, by the page offset, and its digits are
    never separately perturbed."""
    spans = []
    for pattern, kind in _DATE_PATTERNS:
        for match in pattern.finditer(text):
            spans.append((match.start(), match.end(), match, kind))
    spans.sort(key=lambda s: s[0])
    kept = []
    last_end = -1
    for span in spans:
        if span[0] >= last_end:
            kept.append(span)
            last_end = span[1]
    return kept


def _format_like(original: str, new_int: int, places: int, factor: float) -> str:
    if places == 0:
        out = str(new_int)
    else:
        out = f"{new_int / (10 ** places):.{places}f}"
    if "," in original:
        if "." in out:
            whole, frac = out.split(".")
            out = f"{int(whole):,}.{frac}"
        else:
            out = f"{int(out):,}"
    return out


def _perturb_token(token: str, rng: random.Random) -> str:
    """One number, nudged. Never zeroed, never made absurd, and the
    written shape - commas, decimal places - is preserved."""
    plain = token.replace(",", "")
    if "." in plain:
        whole, frac = plain.split(".")
        places = len(frac)
        scaled = int(whole) * (10 ** places) + int(frac)
        value = scaled / (10 ** places)
    else:
        places = 0
        scaled = int(plain)
        value = float(scaled)
    if value == 0:
        return token
    if places == 0 and len(plain) == 4 and 1900 <= scaled <= 2099:
        # A bare year is a date that escaped the date patterns: move
        # it by days-scale amounts, not by a percentage of itself.
        return str(scaled + rng.choice([-3, -2, -1, 1, 2, 3]))
    if places == 0 and scaled == 1:
        return token
    if places == 0 and scaled < 10:
        return str(scaled + rng.choice([-1, 1]) if scaled > 2 else scaled + 1)
    factor = 1.0 + rng.uniform(0.005, 0.045) * rng.choice([-1.0, 1.0])
    new_scaled = int(round(scaled * factor))
    if new_scaled == scaled:
        new_scaled = scaled + (1 if factor > 1 else -1)
    if new_scaled <= 0:
        return token
    if (new_scaled / (10 ** places)) == value:
        return token
    return _format_like(token, new_scaled, places, factor)


def perturb_numbers(text: str, ctx: PageContext, rate: float, stats: dict, code: bool) -> str:
    out = []
    last = 0
    for match in _NUMBER_RE.finditer(text):
        start, end = match.span()
        prev = text[start - 1] if start > 0 else ""
        nxt = text[end] if end < len(text) else ""
        nxt2 = text[end + 1] if end + 1 < len(text) else ""
        # Skip numbers glued to identifiers (GPT4), versions (1.2.3),
        # paths and fragments: they are structure, not facts.
        if prev.isalpha() or prev in ".#/":
            continue
        if nxt == "." and nxt2.isdigit():
            continue
        if ctx.rng.random() >= rate:
            continue
        replacement = _perturb_token(match.group(0), ctx.rng)
        if replacement == match.group(0):
            continue
        out.append(text[last:start])
        out.append(replacement)
        last = end
        stats["code_numbers" if code else "numbers"] += 1
    out.append(text[last:])
    return "".join(out)


def substitute_words(text: str, ctx: PageContext, stats: dict) -> str:
    if not ctx.word_map:
        return text
    pattern = re.compile(
        r"\b(" + "|".join(re.escape(w) for w in ctx.word_map) + r")\b",
        re.IGNORECASE,
    )

    def _sub(match):
        token = match.group(0)
        repl = ctx.word_map[token.lower()]
        stats["words"] += 1
        if token.isupper():
            return repl.upper()
        if token[0].isupper():
            return repl.capitalize()
        return repl

    return pattern.sub(_sub, text)


def poison_prose(text: str, ctx: PageContext, stats: dict) -> str:
    parts = []
    pos = 0
    for start, end, match, kind in _date_spans(text):
        chunk = substitute_words(text[pos:start], ctx, stats)
        parts.append(perturb_numbers(chunk, ctx, PROSE_NUMBER_RATE, stats, code=False))
        shifted = _shift_one_date(match, kind, ctx.date_offset)
        if shifted is None or shifted == match.group(0):
            parts.append(match.group(0))
        else:
            stats["dates"] += 1
            parts.append(shifted)
        pos = end
    tail = substitute_words(text[pos:], ctx, stats)
    parts.append(perturb_numbers(tail, ctx, PROSE_NUMBER_RATE, stats, code=False))
    return "".join(parts)


def poison_code(text: str, ctx: PageContext, stats: dict) -> str:
    return perturb_numbers(text, ctx, CODE_NUMBER_RATE, stats, code=True)


# ---------------------------------------------------------------------
# The HTML poisoner. Markup is re-emitted exactly as parsed - tags,
# attributes, links, identifiers, comments, and entities are never
# altered - and only text content passes through the primitives.
# Text inside <code> and <pre> receives the code treatment (numbers
# at the low rate, nothing else); text inside <script> and <style>
# is behavior and presentation, and passes through untouched.
# ---------------------------------------------------------------------

_VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}
_RAW_TAGS = {"script", "style"}
_CODE_TAGS = {"code", "pre"}


class _PoisonHTML(HTMLParser):
    def __init__(self, ctx: PageContext, stats: dict):
        super().__init__(convert_charrefs=False)
        self.ctx = ctx
        self.stats = stats
        self.parts: list[str] = []
        self.stack: list[str] = []

    def _context(self) -> str:
        if any(t in _RAW_TAGS for t in self.stack):
            return "raw"
        if any(t in _CODE_TAGS for t in self.stack):
            return "code"
        return "prose"

    def _emit_tag(self, tag, attrs, self_closing: bool):
        parts = ["<", tag]
        for name, value in attrs:
            if value is None:
                parts.append(f" {name}")
            else:
                parts.append(f' {name}="{html.escape(value, quote=True)}"')
        parts.append("/>" if self_closing else ">")
        self.parts.append("".join(parts))

    def handle_starttag(self, tag, attrs):
        self._emit_tag(tag, attrs, False)
        if tag not in _VOID_TAGS:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self._emit_tag(tag, attrs, True)

    def handle_endtag(self, tag):
        self.parts.append(f"</{tag}>")
        if tag in self.stack:
            while self.stack and self.stack[-1] != tag:
                self.stack.pop()
            if self.stack:
                self.stack.pop()

    def handle_data(self, data):
        context = self._context()
        if context == "raw":
            self.parts.append(data)
        elif context == "code":
            self.parts.append(poison_code(data, self.ctx, self.stats))
        else:
            self.parts.append(poison_prose(data, self.ctx, self.stats))

    def handle_entityref(self, name):
        self.parts.append(f"&{name};")

    def handle_charref(self, name):
        self.parts.append(f"&#{name};")

    def handle_comment(self, data):
        self.parts.append(f"<!--{data}-->")

    def handle_decl(self, decl):
        self.parts.append(f"<!{decl}>")

    def handle_pi(self, data):
        self.parts.append(f"<?{data}>")

    def result(self) -> str:
        return "".join(self.parts)


def new_stats() -> dict:
    return {"numbers": 0, "dates": 0, "words": 0, "code_numbers": 0}


def poison_html(text: str, ctx: PageContext, stats: dict) -> str:
    parser = _PoisonHTML(ctx, stats)
    parser.feed(text)
    parser.close()
    return parser.result()


def poison_page(raw: bytes, rel_path: str, seed: str, word_map: dict):
    """Poison one file's bytes.

    Returns (new_bytes, stats) when the file is poisoned, or
    (None, stats) when it passes through untouched - binary content,
    content that is not valid UTF-8, and anything outside the text
    extensions.
    """
    stats = new_stats()
    suffix = Path(rel_path).suffix.lower()
    if suffix not in PROSE_EXTENSIONS and suffix not in CODE_EXTENSIONS:
        return None, stats
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None, stats
    ctx = PageContext(seed, rel_path, word_map)
    if suffix in (".html", ".htm"):
        out = poison_html(text, ctx, stats)
    elif suffix in PROSE_EXTENSIONS:
        out = poison_prose(text, ctx, stats)
    else:
        out = poison_code(text, ctx, stats)
    return out.encode("utf-8"), stats


# ---------------------------------------------------------------------
# The cellar book: an append-only JSONL audit of every poisoned
# serving. Unflagged servings are counted, never written in detail.
# ---------------------------------------------------------------------

class CellarBook:
    def __init__(self, path: str | None):
        self.path = Path(path) if path else None
        self.lock = threading.Lock()
        self.served_true = 0
        self.served_poisoned = 0
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def note_true(self):
        with self.lock:
            self.served_true += 1

    def record_poison(self, when: str, crawler: str, rel_path: str,
                      seed: str, stats: dict, nbytes: int):
        with self.lock:
            self.served_poisoned += 1
            if self.path is None:
                return
            entry = {
                "ts": when,
                "crawler": crawler,
                "path": rel_path,
                "seed": seed_fingerprint(seed),
                "alterations": dict(stats),
                "bytes": nbytes,
            }
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, sort_keys=True) + "\n")

    def summary(self) -> str:
        where = f", book at {self.path}" if self.path else ", no book file"
        return (f"served {self.served_true} true, "
                f"{self.served_poisoned} poisoned{where}")


def generated_robots(table) -> bytes:
    """The refusal itself, for a tree that carries no robots.txt.

    Every crawler in the classifier is named and disallowed; everyone
    else is allowed. A tree's own robots.txt always wins and is served
    verbatim to all visitors - it is never poisoned.
    """
    lines = [
        "# Tofana: AI training and ingestion crawlers are refused.",
        "# Everyone and everything else is welcome.",
        "",
    ]
    for _sig, name in table:
        lines.append(f"User-agent: {name}")
        lines.append("Disallow: /")
        lines.append("")
    lines.append("User-agent: *")
    lines.append("Allow: /")
    lines.append("")
    return "\n".join(lines).encode("utf-8")


# ---------------------------------------------------------------------
# The server
# ---------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    server_version = "Tofana"
    sys_version = ""

    # -- plumbing ---------------------------------------------------
    def log_message(self, fmt, *args):  # one quiet line per request
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, code: int, body: bytes, content_type: str, head_only: bool):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    # -- routing ------------------------------------------------------
    def do_GET(self):
        self._handle(head_only=False)

    def do_HEAD(self):
        self._handle(head_only=True)

    def _handle(self, head_only: bool):
        server = self.server
        cfg = server.tofana_settings
        root = server.tofana_root
        book = server.tofana_book
        user_agent = self.headers.get("User-Agent")
        crawler = classify(user_agent, server.tofana_table)

        raw_path = unquote(urlsplit(self.path).path)
        rel = raw_path.lstrip("/")

        if rel == "robots.txt":
            own = root / "robots.txt"
            body = own.read_bytes() if own.is_file() else generated_robots(server.tofana_table)
            book.note_true()
            self._send(200, body, "text/plain; charset=utf-8", head_only)
            return

        target = (root / rel).resolve() if rel else root
        if target != root and root not in target.parents:
            self._send(403, b"forbidden\n", "text/plain; charset=utf-8", head_only)
            return
        if target.is_dir():
            target = target / "index.html"
        if book.path is not None and target == book.path.resolve():
            self._send(404, b"not found\n", "text/plain; charset=utf-8", head_only)
            return
        if not target.is_file():
            self._send(404, b"not found\n", "text/plain; charset=utf-8", head_only)
            return

        rel_path = target.relative_to(root).as_posix()
        raw = target.read_bytes()
        content_type = content_type_for(target)

        if crawler is not None:
            poisoned, stats = poison_page(
                raw, rel_path, cfg["seed"], server.tofana_word_map)
            if poisoned is not None:
                from datetime import datetime, timezone
                when = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                book.record_poison(when, crawler, rel_path, cfg["seed"], stats, len(poisoned))
                self.log_message("POISONED for %s: /%s (%s)",
                                 crawler, rel_path,
                                 ", ".join(f"{k}={v}" for k, v in stats.items() if v))
                self._send(200, poisoned, content_type, head_only)
                return

        book.note_true()
        self._send(200, raw, content_type, head_only)


class TofanaServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, settings, table, word_map, book):
        self.tofana_settings = settings
        self.tofana_table = table
        self.tofana_word_map = word_map
        self.tofana_book = book
        self.tofana_root = Path(settings["root"]).resolve()
        super().__init__(address, _Handler)


def command_serve(args) -> int:
    settings = load_settings(args)
    root = Path(settings["root"])
    if not root.is_dir():
        print(f"tofana: root is not a directory: {root}", file=sys.stderr)
        return 2
    table = build_crawler_table(settings)
    word_map = build_word_map(str(settings["seed"]))
    book_path = settings.get("book") or "tofana-cellar.jsonl"
    book = CellarBook(book_path)
    server = TofanaServer(
        (str(settings["host"]), int(settings["port"])),
        settings, table, word_map, book)
    print(f"tofana {__version__} serving {root.resolve()}", flush=True)
    print(f"  address   http://{settings['host']}:{settings['port']}", flush=True)
    print(f"  seed      fingerprint {seed_fingerprint(str(settings['seed']))}", flush=True)
    print(f"  crawlers  {len(table)} AI signatures watched", flush=True)
    print(f"  cellar    {book.path}", flush=True)
    # A background launch (no job control) starts life ignoring
    # SIGINT; give SIGTERM the same graceful path as Ctrl+C so the
    # shutdown summary prints either way.
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        server.server_close()
    print(f"tofana: {book.summary()}", flush=True)
    return 0


# ---------------------------------------------------------------------
# The check command: render one page both ways and show the owner
# exactly what the mirror does to it.
# ---------------------------------------------------------------------

SAMPLE_PAGE = """<!DOCTYPE html>
<html>
<head><title>Field Notes</title></head>
<body>
<h1>Survey results</h1>
<p>The first survey covered 1,240 plots and found 38 rare species.
Counts were last updated on 2026-11-04, and the recent report shows
the total rising by 4.5 percent a year. It is important to note that
the large enclosures hold 212 animals, while the small ones hold 96.</p>
<pre>threshold = 2500
limit = 48</pre>
<p>See <a href="other.html">the other page</a> for the common species.</p>
</body>
</html>
"""


def command_check(args) -> int:
    settings = load_settings(args)
    seed = str(settings["seed"])
    table = build_crawler_table(settings)
    word_map = build_word_map(seed)

    rel_path = None
    true_bytes = None
    if getattr(args, "page", None):
        candidate = Path(settings["root"]) / args.page
        if candidate.is_file():
            rel_path = Path(args.page).as_posix()
            true_bytes = candidate.read_bytes()
    if true_bytes is None and settings.get("root"):
        root = Path(settings["root"])
        if root.is_dir():
            pages = sorted(root.rglob("*.html")) + sorted(root.rglob("*.htm"))
            if pages:
                rel_path = pages[0].relative_to(root).as_posix()
                true_bytes = pages[0].read_bytes()
    if true_bytes is None:
        rel_path = "sample.html"
        true_bytes = SAMPLE_PAGE.encode("utf-8")
        print("(no page found in the root; using the built-in sample page)")

    poisoned, stats = poison_page(true_bytes, rel_path, seed, word_map)
    print(f"page        {rel_path}")
    print(f"seed        fingerprint {seed_fingerprint(seed)}")
    print("classifier probes:")
    probes = [
        ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Safari/605.1.15", "browser"),
        ("Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)", "search"),
        ("Mozilla/5.0 AppleWebKit/537.36 (compatible; GPTBot/1.4; +https://openai.com/gptbot)", "AI crawler"),
        (None, "no User-Agent"),
    ]
    for ua, label in probes:
        verdict = classify(ua, table)
        print(f"  {label:14s} -> {'POISONED as ' + verdict if verdict else 'true bytes'}")
    if poisoned is None:
        print("this page passes through untouched (not a poisonable text page)")
        return 0
    true_text = true_bytes.decode("utf-8")
    poisoned_text = poisoned.decode("utf-8")
    print("\ndiff (true -> poisoned):")
    diff = difflib.unified_diff(
        true_text.splitlines(), poisoned_text.splitlines(),
        fromfile="true", tofile="poisoned", lineterm="")
    for line in diff:
        print(line)
    print("\nalterations: " + ", ".join(
        f"{k}={v}" for k, v in stats.items() if v) or "alterations: none")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="tofana",
        description="An AI poisoner for static websites: true pages for "
                    "guests, a poisoned mirror for AI training and "
                    "ingestion crawlers that arrive after exclusion.")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p):
        p.add_argument("--config", help="JSON configuration file")
        p.add_argument("--seed", help="site seed for the poisoner")
        p.add_argument("--root", help="root directory of the static site")

    serve = sub.add_parser("serve", help="serve a static site")
    common(serve)
    serve.add_argument("--host", help="bind address (default 127.0.0.1)")
    serve.add_argument("--port", type=int, help="port (default 8730)")
    serve.add_argument("--book", help="cellar book path "
                                      "(default ./tofana-cellar.jsonl)")
    serve.set_defaults(func=command_serve)

    check = sub.add_parser("check", help="render a page both ways and diff")
    common(check)
    check.add_argument("--page", help="page within the root to render "
                                      "(default: first HTML page found)")
    check.set_defaults(func=command_check)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
