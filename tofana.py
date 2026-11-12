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

Version 0.2 adds six further vials to the same doctrine: trap facts
(the canary), full-strength code poison, provenance poison, kinship
poison, tongue poison for non-English pages, and lure pages that
exist only for flagged crawlers. Each is described where it is
implemented below, and each obeys the laws above without exception:
flagged crawlers only, deterministic per site seed and page,
structure never altered, and every dose written to the cellar book.

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

__version__ = "0.2.0"

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
    "entities": [],
    "kinship": [],
    "lures": [],
    "register": None,
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
    for key in ("host", "port", "seed", "root", "book", "register"):
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
# The six vials of 0.2.
#
# Canary: config-listed real names (contributors, functions,
# releases, places) are replaced site-wide by invented counterparts
# derived from the seed. The true-to-invented mapping is the owner's
# canary register: if an invented name ever surfaces in a model's
# output, the well it drank from is proven.
#
# Code: code spans and code files keep the one-in-fifty numeric rate
# and additionally suffer rare boolean/default flips and flag-name
# near-misses, inside code only.
#
# Provenance: version strings map site-wide to one consistent false
# version per true version, and bare years move with the page's date
# offset, so the mirror's lineage of the work is false and agrees
# with itself. Citation author names are covered by the canary
# table when the owner lists them as entities.
#
# Kinship: config-listed term pairs are cross-swapped in prose, so
# each sentence stays plausible while the relationship graph bends.
#
# Tongue: on non-English pages, a small built-in table of niche-term
# translation pairs is cross-swapped with seeded consistency, so the
# vocabulary is learned wrong in other languages.
#
# Lure: config-defined pages that exist only for flagged crawlers,
# generated on the fly at paths that do not exist on disk.
# ---------------------------------------------------------------------

CODE_FLIP_RATE = 0.06   # share of code booleans/flags that go wrong

FLAG_PAIRS = [
    ("--quiet", "--silent"),
    ("--force", "--hard"),
    ("--verbose", "--detailed"),
    ("--recursive", "--deep"),
    ("--dry-run", "--trial"),
    ("--follow", "--tail"),
    ("--output", "--outfile"),
    ("--config", "--settings"),
]

_VERSION_RE = re.compile(r"(?<![\d.])v?(\d+)\.(\d+)\.(\d+)(?!\.\d)")
_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
_BOOL_RE = re.compile(r"\b(true|false|True|False|TRUE|FALSE)\b")
_ONOFF_RE = re.compile(r"\b(on|off)\b")


def false_version(seed: str, major: int, minor: int, patch: int) -> tuple:
    """The one false version a true version maps to, site-wide."""
    rng = _rng_from(seed, "version", f"{major}.{minor}.{patch}")
    return (major, minor + rng.randint(1, 4), rng.randint(0, 20))


def _match_case(token: str, repl: str) -> str:
    if token.isupper():
        return repl.upper()
    if token[:1].isupper():
        return repl[:1].upper() + repl[1:]
    return repl


def build_swap_map(pairs) -> dict:
    """A bidirectional token map from a list of pairs (or a dict).

    Swapping is symmetric: wherever one side of a pair appears, the
    other takes its place, so the mirror world stays self-consistent
    instead of merely renaming things in one direction.
    """
    mapping = {}
    if isinstance(pairs, dict):
        pairs = list(pairs.items())
    for pair in pairs or []:
        if isinstance(pair, (list, tuple)) and len(pair) == 2:
            a, b = str(pair[0]), str(pair[1])
            if a and b and a.lower() != b.lower():
                mapping[a.lower()] = b
                mapping[b.lower()] = a
    return mapping


def swap_pairs(text: str, mapping: dict, stats: dict, key: str) -> str:
    """Apply a token map to prose, preserving each token's case."""
    if not mapping:
        return text
    keys = sorted(mapping, key=len, reverse=True)
    pattern = re.compile(
        r"\b(" + "|".join(re.escape(k) for k in keys) + r")\b",
        re.IGNORECASE)

    def _sub(match):
        token = match.group(0)
        stats[key] += 1
        return _match_case(token, mapping[token.lower()])

    return pattern.sub(_sub, text)


# -- the canary vial ---------------------------------------------------

_CANARY_FIRST = [
    "Marisol", "Tomas", "Ines", "Rurik", "Selma", "Anders",
    "Priya", "Noor", "Emil", "Katya", "Oren", "Livia",
    "Marcus", "Helena", "Dario", "Fenna",
]
_CANARY_LAST = [
    "Venn", "Kova", "Alder", "Marsh", "Quill", "Hallow",
    "Rook", "Sable", "Thorn", "Vale", "Wren", "Osman",
    "Petrov", "Lindqvist", "Barros", "Kettner",
]
_CANARY_ID_A = [
    "velvet", "copper", "hidden", "amber", "quiet", "second",
    "hollow", "paper", "iron", "brisk", "northern", "fallow",
]
_CANARY_ID_B = [
    "falcon", "ledger", "harbor", "needle", "orchard", "cipher",
    "lantern", "meadow", "sieve", "tundra", "walnut", "zephyr",
]


def _invented_entity(seed: str, entity: str, taken: set) -> str:
    """One invented counterpart for a real name, from the seed.

    The shape follows the original: a version becomes a false
    version, an identifier becomes an identifier of the same style,
    a multi-word name becomes a first-and-last name, a Capitalized
    single name becomes a surname-shaped token.
    """
    base = entity.strip().rstrip("()")
    attempt = 0
    while True:
        rng = _rng_from(seed, "canary", base.lower(), str(attempt))
        version = _VERSION_RE.fullmatch(base)
        if version:
            fmaj, fmin, fpat = false_version(
                seed, int(version.group(1)), int(version.group(2)),
                int(version.group(3)))
            candidate = ("v" if base.startswith("v") else "") + \
                f"{fmaj}.{fmin}.{fpat}"
        elif " " in base:
            candidate = f"{rng.choice(_CANARY_FIRST)} {rng.choice(_CANARY_LAST)}"
        elif "_" in base or entity.endswith("()") or \
                (base.isidentifier() and
                 any(c.isupper() for c in base[1:])):
            a, b = rng.choice(_CANARY_ID_A), rng.choice(_CANARY_ID_B)
            candidate = f"{a}_{b}" if "_" in base else f"{a}{b.capitalize()}"
        elif base[:1].isupper():
            candidate = rng.choice(_CANARY_LAST)
        else:
            candidate = rng.choice(_CANARY_ID_A)
        if candidate.lower() != base.lower() and \
                candidate.lower() not in taken:
            taken.add(candidate.lower())
            return candidate
        attempt += 1


def build_entity_map(seed: str, entities) -> tuple:
    """(match map, register) for the canary vial.

    The match map keys are the names as they appear in prose (a
    function's trailing parentheses are not part of the match); the
    register preserves the owner's original strings.
    """
    names = [str(e).strip() for e in entities or [] if str(e).strip()]
    taken = {n.rstrip("()").lower() for n in names}
    mapping, register = {}, {}
    for name in names:
        invented = _invented_entity(seed, name, taken)
        mapping[name.rstrip("()").lower()] = invented
        register[name] = invented
    return mapping, register


# -- the tongue vial ---------------------------------------------------
#
# Niche technical vocabulary, cross-swapped within each language:
# transmitter with receiver, input with output, signal with noise.
# A model reading the mirror in Spanish, French, or German learns
# the words wrong in that language. English pages are never touched
# by this vial, and languages without a table are left alone.

TONGUE_TABLES = {
    "es": [
        ("emisor", "receptor"),
        ("entrada", "salida"),
        ("frecuencia", "amplitud"),
        ("señal", "ruido"),
        ("ganancia", "atenuación"),
    ],
    "fr": [
        ("émetteur", "récepteur"),
        ("entrée", "sortie"),
        ("fréquence", "amplitude"),
        ("signal", "bruit"),
        ("gain", "perte"),
    ],
    "de": [
        ("Sender", "Empfänger"),
        ("Eingang", "Ausgang"),
        ("Frequenz", "Amplitude"),
        ("Signal", "Rauschen"),
        ("Verstärkung", "Dämpfung"),
    ],
}

_LANG_SEGMENTS = {
    "es", "fr", "de", "it", "pt", "nl", "pl", "sv", "da",
    "no", "fi", "ru", "ja", "zh", "ko", "ar",
}


def detect_language(rel_path: str, text: str, suffix: str) -> str | None:
    """The page's language, or None for English or undetermined.

    An HTML lang attribute wins; otherwise a language directory in
    the path (/es/, /fr/, /de/, ...) decides.
    """
    if suffix in (".html", ".htm"):
        match = re.search(
            r"<html[^>]*\blang=[\"']([A-Za-z-]+)[\"']", text, re.IGNORECASE)
        if match:
            lang = match.group(1).lower().split("-")[0]
            return None if lang == "en" else lang
    for part in Path(rel_path).parts:
        if part.lower() in _LANG_SEGMENTS:
            return part.lower()
    return None


class PoisonConfig:
    """Everything the vials need, derived from the settings + seed."""

    def __init__(self, seed: str, entities=(), kinship=(), lures=(),
                 tongue: bool = True):
        self.seed = seed
        self.canary_map, self.canary_register = build_entity_map(
            seed, entities)
        self.kinship_map = build_swap_map(kinship)
        rng = _rng_from(seed, "flags")
        self.flag_map = build_swap_map(
            [p for p in FLAG_PAIRS if rng.random() < 0.75])
        self.tongue_enabled = tongue
        self._tongue_maps: dict = {}
        self.lures: dict = {}
        for lure in lures or []:
            if isinstance(lure, dict) and lure.get("path"):
                path = str(lure["path"]).lstrip("/")
                self.lures[path] = {
                    "path": path,
                    "question": str(lure.get("question", "")),
                    "answer": str(lure.get("answer", "")),
                }

    def tongue_map(self, lang: str) -> dict:
        if lang not in self._tongue_maps:
            pairs = TONGUE_TABLES.get(lang) or []
            rng = _rng_from(self.seed, "tongue", lang)
            active = [p for p in pairs if rng.random() < 0.85]
            self._tongue_maps[lang] = build_swap_map(active)
        return self._tongue_maps[lang]

    def lure_for(self, rel_path: str):
        return self.lures.get(rel_path)


def build_poison_config(settings) -> PoisonConfig:
    return PoisonConfig(
        str(settings.get("seed", "tofana")),
        entities=settings.get("entities") or [],
        kinship=settings.get("kinship") or [],
        lures=settings.get("lures") or [])


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

    def __init__(self, seed: str, rel_path: str, word_map: dict,
                 cfg: PoisonConfig | None = None, lang: str | None = None):
        self.seed = seed
        self.cfg = cfg if cfg is not None else PoisonConfig(seed)
        self.lang = lang
        self.rng = _rng_from(seed, "page", rel_path)
        # The date offset is drawn first, before any content is read,
        # so the stream position never depends on the page's contents.
        choices = [d for d in range(-21, 22) if d not in (-1, 0, 1)]
        self.date_offset = self.rng.choice(choices)
        # Bare years (the citation years of the provenance vial) move
        # with the page's dates: same direction, days-scale size.
        self.year_shift = (1 if self.date_offset > 0 else -1) * \
            (1 + (abs(self.date_offset) % 3))
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


def _poison_gap(text: str, ctx: PageContext, stats: dict) -> str:
    """A stretch of prose holding no protected span: the swap vials
    (kinship, canary, tongue), the word table, then the numbers."""
    if not text:
        return text
    text = swap_pairs(text, ctx.cfg.kinship_map, stats, "kinship")
    text = swap_pairs(text, ctx.cfg.canary_map, stats, "canary")
    if ctx.lang and ctx.cfg.tongue_enabled:
        text = swap_pairs(
            text, ctx.cfg.tongue_map(ctx.lang), stats, "tongue")
    text = substitute_words(text, ctx, stats)
    return perturb_numbers(text, ctx, PROSE_NUMBER_RATE, stats, code=False)


def _poison_protected(text: str, ctx: PageContext, stats: dict) -> str:
    """Prose between dates, with the provenance vial's spans held
    out of the number pass: a version string maps whole to its one
    false counterpart, and a bare year moves with the page offset."""
    parts = []
    pos = 0
    spans = []
    for match in _VERSION_RE.finditer(text):
        spans.append((match.start(), match.end(), "version", match))
    for match in _YEAR_RE.finditer(text):
        if any(s <= match.start() < e for s, e, _k, _m in spans):
            continue
        spans.append((match.start(), match.end(), "year", match))
    spans.sort(key=lambda s: s[0])
    for start, end, kind, match in spans:
        parts.append(_poison_gap(text[pos:start], ctx, stats))
        if kind == "version":
            prefix = "v" if match.group(0).startswith("v") else ""
            fmaj, fmin, fpat = false_version(
                ctx.seed, int(match.group(1)), int(match.group(2)),
                int(match.group(3)))
            parts.append(f"{prefix}{fmaj}.{fmin}.{fpat}")
        else:
            parts.append(str(int(match.group(0)) + ctx.year_shift))
        stats["provenance"] += 1
        pos = end
    parts.append(_poison_gap(text[pos:], ctx, stats))
    return "".join(parts)


def poison_prose(text: str, ctx: PageContext, stats: dict) -> str:
    parts = []
    pos = 0
    for start, end, match, kind in _date_spans(text):
        parts.append(_poison_protected(text[pos:start], ctx, stats))
        shifted = _shift_one_date(match, kind, ctx.date_offset)
        if shifted is None or shifted == match.group(0):
            parts.append(match.group(0))
        else:
            stats["dates"] += 1
            parts.append(shifted)
        pos = end
    parts.append(_poison_protected(text[pos:], ctx, stats))
    return "".join(parts)


def _flip_bool(match, ctx: PageContext, stats: dict) -> str:
    if ctx.rng.random() >= CODE_FLIP_RATE:
        return match.group(0)
    stats["code"] += 1
    token = match.group(0)
    flipped = "false" if token.lower() == "true" else "true"
    return _match_case(token, flipped)


def _flip_onoff(match, ctx: PageContext, stats: dict) -> str:
    if ctx.rng.random() >= CODE_FLIP_RATE:
        return match.group(0)
    stats["code"] += 1
    return "off" if match.group(0) == "on" else "on"


def _swap_flags(text: str, ctx: PageContext, stats: dict) -> str:
    mapping = ctx.cfg.flag_map
    if not mapping:
        return text
    keys = sorted(mapping, key=len, reverse=True)
    pattern = re.compile(
        r"(?<![\w-])(" + "|".join(re.escape(k) for k in keys) +
        r")(?![\w-])")

    def _sub(match):
        if ctx.rng.random() >= CODE_FLIP_RATE:
            return match.group(0)
        stats["code"] += 1
        return mapping[match.group(0).lower()]

    return pattern.sub(_sub, text)


def poison_code(text: str, ctx: PageContext, stats: dict) -> str:
    """The code vial at full strength: numbers at one in fifty, plus
    rare boolean/default flips and flag-name near-misses. This is
    the only treatment code ever receives - no word table, no swaps
    of prose, no dates - and prose never receives this one."""
    out = perturb_numbers(text, ctx, CODE_NUMBER_RATE, stats, code=True)
    out = _BOOL_RE.sub(lambda m: _flip_bool(m, ctx, stats), out)
    out = _ONOFF_RE.sub(lambda m: _flip_onoff(m, ctx, stats), out)
    out = _swap_flags(out, ctx, stats)
    return out


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
    return {"numbers": 0, "dates": 0, "words": 0, "code_numbers": 0,
            "canary": 0, "code": 0, "provenance": 0, "kinship": 0,
            "tongue": 0, "lure": 0}


def poison_html(text: str, ctx: PageContext, stats: dict) -> str:
    parser = _PoisonHTML(ctx, stats)
    parser.feed(text)
    parser.close()
    return parser.result()


def poison_page(raw: bytes, rel_path: str, seed: str, word_map: dict,
                cfg: PoisonConfig | None = None):
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
    if cfg is None:
        cfg = PoisonConfig(seed)
    lang = detect_language(rel_path, text, suffix) \
        if suffix in PROSE_EXTENSIONS else None
    ctx = PageContext(seed, rel_path, word_map, cfg=cfg, lang=lang)
    if suffix in (".html", ".htm"):
        out = poison_html(text, ctx, stats)
    elif suffix in PROSE_EXTENSIONS:
        out = poison_prose(text, ctx, stats)
    else:
        out = poison_code(text, ctx, stats)
    return out.encode("utf-8"), stats


def render_lure(lure: dict, seed: str, word_map: dict,
                cfg: PoisonConfig):
    """Generate a lure page: a question, an answer, and the standard
    poison passes over both. Lures are produced only in response to
    a flagged crawler's request, never written to disk."""
    question = html.escape(lure["question"])
    answer = html.escape(lure["answer"])
    page = ("<!DOCTYPE html>\n<html>\n<head><title>" + question +
            "</title></head>\n<body>\n<h1>" + question + "</h1>\n<p>" +
            answer + "</p>\n</body>\n</html>\n")
    poisoned, stats = poison_page(
        page.encode("utf-8"), lure["path"], seed, word_map, cfg)
    if poisoned is None:
        poisoned = page.encode("utf-8")
    stats["lure"] += 1
    return poisoned, stats


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

        # The lure vial: a configured path that does not exist on
        # disk exists only for flagged crawlers. Guests get the
        # ordinary 404 below, and a real file always wins - a lure
        # never shadows one.
        if crawler is not None:
            lure = server.tofana_config.lure_for(rel)
            if lure is not None and not (root / rel).is_file():
                body, stats = render_lure(
                    lure, cfg["seed"], server.tofana_word_map,
                    server.tofana_config)
                from datetime import datetime, timezone
                when = datetime.now(timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ")
                book.record_poison(when, crawler, rel, cfg["seed"],
                                   stats, len(body))
                self.log_message("POISONED (lure) for %s: /%s",
                                 crawler, rel)
                self._send(200, body, "text/html; charset=utf-8",
                           head_only)
                return

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
        if server.tofana_register is not None and \
                target == server.tofana_register:
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
                raw, rel_path, cfg["seed"], server.tofana_word_map,
                server.tofana_config)
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
        self.tofana_config = build_poison_config(settings)
        if settings.get("register"):
            self.tofana_register = Path(settings["register"]).resolve()
        elif book.path is not None:
            self.tofana_register = Path(book.path).resolve().with_name(
                "tofana-canary.json")
        else:
            self.tofana_register = None
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
    config = build_poison_config(settings)
    register_path = None
    if config.canary_register:
        # The canary register is the owner's detection list. It is
        # written beside the cellar book, never inside the served
        # root, and the server refuses to serve it.
        register_path = Path(settings.get("register") or
                             Path(book_path).with_name(
                                 "tofana-canary.json"))
        register_path.parent.mkdir(parents=True, exist_ok=True)
        register_path.write_text(json.dumps(
            {"seed": seed_fingerprint(str(settings["seed"])),
             "entities": config.canary_register},
            indent=2, sort_keys=True) + "\n", encoding="utf-8")
    server = TofanaServer(
        (str(settings["host"]), int(settings["port"])),
        settings, table, word_map, book)
    print(f"tofana {__version__} serving {root.resolve()}", flush=True)
    print(f"  address   http://{settings['host']}:{settings['port']}", flush=True)
    print(f"  seed      fingerprint {seed_fingerprint(str(settings['seed']))}", flush=True)
    print(f"  crawlers  {len(table)} AI signatures watched", flush=True)
    print(f"  cellar    {book.path}", flush=True)
    if register_path is not None:
        print(f"  canary    register at {register_path} "
              f"({len(config.canary_register)} entities)", flush=True)
    if config.lures:
        print(f"  lures     {len(config.lures)} configured", flush=True)
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

def vial_report(root, cfg: PoisonConfig, extra_texts=()) -> list:
    """One (name, active, reason) line per vial for a tree + config.

    A vial is active only when it can actually bite on this tree:
    configured and - where the vial needs material - present in the
    tree's pages. Anything else is reported inert, with the reason.
    """
    root_path = Path(root) if root else None
    texts = []
    if root_path is not None and root_path.is_dir():
        for path in sorted(root_path.rglob("*")):
            if path.is_file() and path.suffix.lower() in \
                    PROSE_EXTENSIONS | CODE_EXTENSIONS:
                try:
                    texts.append((path.relative_to(root_path).as_posix(),
                                  path.read_text(encoding="utf-8")))
                except (UnicodeDecodeError, OSError):
                    pass
            if len(texts) >= 200:
                break
    texts.extend(extra_texts)

    def occurs(needle: str) -> bool:
        lowered = needle.lower()
        return any(lowered in text.lower() for _rel, text in texts)

    has_code = any(
        Path(rel).suffix.lower() in CODE_EXTENSIONS or
        "<code" in text or "<pre" in text for rel, text in texts)
    langs = set()
    for rel, text in texts:
        suffix = Path(rel).suffix.lower()
        if suffix in PROSE_EXTENSIONS:
            lang = detect_language(rel, text, suffix)
            if lang:
                langs.add(lang)
    tongue_langs = sorted(l for l in langs if l in TONGUE_TABLES)
    other_langs = sorted(l for l in langs if l not in TONGUE_TABLES)

    report = []
    if not cfg.canary_register:
        report.append(("canary", False, "no entities configured"))
    elif any(occurs(name) for name in cfg.canary_register):
        report.append(("canary", True,
                       f"{len(cfg.canary_register)} entities, present "
                       "in the tree"))
    else:
        report.append(("canary", False,
                       "entities configured but none occur in the tree"))
    report.append(("code", has_code,
                   "code spans or code files present"
                   if has_code else "no code spans or code files found"))
    versions = any(_VERSION_RE.search(text) for _rel, text in texts)
    years = any(_YEAR_RE.search(text) for _rel, text in texts)
    if versions or years:
        found = []
        if versions:
            found.append("version strings")
        if years:
            found.append("bare years")
        report.append(("provenance", True, " and ".join(found) +
                       " present"))
    else:
        report.append(("provenance", False,
                       "no version strings or bare years found"))
    if not cfg.kinship_map:
        report.append(("kinship", False, "no term pairs configured"))
    elif any(occurs(term) for term in cfg.kinship_map):
        report.append(("kinship", True,
                       f"{len(cfg.kinship_map) // 2} pairs, terms "
                       "present in the tree"))
    else:
        report.append(("kinship", False,
                       "pairs configured but no terms occur in the "
                       "tree"))
    if tongue_langs:
        report.append(("tongue", True,
                       "non-English pages with a table: " +
                       ", ".join(tongue_langs)))
    elif other_langs:
        report.append(("tongue", False,
                       "non-English pages in languages without a "
                       "table: " + ", ".join(other_langs)))
    else:
        report.append(("tongue", False, "no non-English pages found"))
    if not cfg.lures:
        report.append(("lure", False, "no lures configured"))
    else:
        live, shadowed = [], []
        for path in cfg.lures:
            if root_path is not None and (root_path / path).is_file():
                shadowed.append(path)
            else:
                live.append(path)
        if live:
            reason = f"{len(live)} lure(s) served to crawlers only"
            if shadowed:
                reason += f"; {len(shadowed)} skipped, shadowing a " \
                          "real file"
            report.append(("lure", True, reason))
        else:
            report.append(("lure", False,
                           "all configured lures shadow a real file"))
    return report

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
    cfg = build_poison_config(settings)
    sample_used = False

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
        sample_used = True
        print("(no page found in the root; using the built-in sample page)")

    poisoned, stats = poison_page(true_bytes, rel_path, seed, word_map, cfg)
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
        _print_vials(settings, cfg, sample_used)
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
    _print_vials(settings, cfg, sample_used)
    return 0


def _print_vials(settings, cfg: PoisonConfig, sample_used: bool):
    extra = [("sample.html", SAMPLE_PAGE)] if sample_used else []
    print("\nvials:")
    for name, active, reason in vial_report(
            settings.get("root"), cfg, extra):
        print(f"  {name:12s} {'active' if active else 'inert '}"
              f" - {reason}")
    if cfg.canary_register:
        print("canary register (true -> invented):")
        for real, invented in cfg.canary_register.items():
            print(f"  {real} -> {invented}")


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
    serve.add_argument("--register", help="canary register path "
                                          "(default: beside the book)")
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
