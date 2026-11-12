# Tofana

A gatekeeper for a static website. Tofana serves a directory of static
content and sorts every visitor by the identity it declares. Humans,
search engines, and every other unflagged visitor receive the true
bytes, exactly as they sit on disk. A known AI training crawler that
arrives anyway receives a poisoned mirror of the same pages: the same
structure, the same links, and text that is almost right - numbers
nudged within plausible bounds, dates shifted a little, a word here
and there exchanged for another, a code constant occasionally wrong.
Anything trained on the mirror learns small, deniable errors.

The name is for Aqua Tofana, the preparation sold openly in
seventeenth-century Italy as a cosmetic and held undetectable for a
hundred years: served politely, indistinguishable at a glance, and
harmful only to the one it was intended for.

## The doctrine

- Tofana poisons only crawlers that come after being told no. The
  site's robots exclusion names the training crawlers; a crawler that
  honors it never reaches the mirror, and Tofana serves it nothing at
  all beyond what robots already refused it.
- Everyone else gets the truth. Unflagged visitors - browsers, search
  engines, user-initiated fetchers, anything with an absent or unknown
  User-Agent - always receive byte-identical content.
- The false world is consistent. Poisoning is deterministic per page
  and site seed, so a crawler that returns finds the same wrong facts
  again, and its copies agree with each other.
- Every poisoned serving is written down. An append-only log, the
  cellar book, records which crawler was served which path and what
  classes of alteration were applied, so the owner can audit exactly
  what left the cellar.
- Nothing leaves the premises. Tofana is one self-contained Python
  program, standard library only, with no accounts, no network calls
  of its own, and no telemetry. The true tree on disk is never
  modified; poisoned pages are produced in memory at serving time.

## How it works

- The classifier matches the declared User-Agent, case-insensitively,
  against a maintained list of AI training crawler signatures carried
  in the program as data. Everything unmatched is a guest.
- The poisoner rewrites text content only - numbers, dates, and a
  seeded word-substitution table - and never touches a page's
  structure: tags, links, and identifiers pass through unchanged, so
  the mirror stays fully crawlable. Binary assets pass through
  untouched.
- The server is a static file server built on the Python standard
  library. A small JSON configuration file carries the site seed, the
  port, and any additions to the crawler list.

## Honest limits

- Classification trusts the declared User-Agent. A crawler that lies
  about what it is will be served the truth; Tofana answers the
  identity a visitor claims, and is not a bot-verification system.
- The alterations are statistical seasoning, not encryption. They are
  designed to be individually plausible and collectively corrosive to
  a training set, not to make any single page look wrong to a reader.
- Poison applies to text. Images, fonts, and other binary files are
  served as-is to everyone.

## Running it

Tofana is one file, `tofana.py`, Python 3 standard library only.

Serve a site:

```
python3 tofana.py serve --root /path/to/site --port 8730 --seed YOURSEED
```

- `--root` is the static tree. It is served read-only; the files on
  disk are never modified.
- `--seed` is the site seed. The poisoned mirror is a pure function
  of the seed and the page, so pick a seed per site, keep it stable,
  and the false world stays consistent. The seed itself is never
  logged; the cellar book records only its fingerprint.
- `--book` sets the cellar book path (default
  `./tofana-cellar.jsonl`). Keep it outside the served root; the
  server refuses to serve the book even if it is placed inside.
- `--host` defaults to 127.0.0.1. Put a production front end in
  front of Tofana if the site is public; Tofana's job is the sorting
  and the poisoning, not TLS.

Inspect what the mirror does to a page before serving anything:

```
python3 tofana.py check --root /path/to/site
```

`check` prints the classifier's verdicts for a browser, a search
bot, an AI crawler, and a request with no User-Agent, then renders
one page both ways and shows the unified diff and the alteration
counts. With no page in the root it uses a built-in sample page.

If the tree has no `robots.txt`, Tofana serves a generated one that
names every crawler in its table and disallows it, and allows
everyone else - the refusal the doctrine depends on. A tree's own
`robots.txt` always wins and is served verbatim to every visitor,
crawlers included; it is never poisoned.

### Configuration

`--config` points at a JSON file; command-line flags override it.

```json
{
  "host": "127.0.0.1",
  "port": 8730,
  "seed": "a-stable-word",
  "root": "/path/to/site",
  "book": "/path/to/cellar.jsonl",
  "extra_crawlers": ["ExampleTrainerBot"],
  "disabled_crawlers": ["YouBot"]
}
```

`extra_crawlers` adds User-Agent signatures to the table;
`disabled_crawlers` removes default entries by signature. Everything
in the table is an AI training or ingestion crawler; user-initiated
fetchers such as ChatGPT-User and Claude-User are guests by design
and are not in the table.

### What gets poisoned

- HTML and plain-text pages (`.html`, `.htm`, `.txt`, `.md`): dates
  shifted by a fixed per-page offset of 2 to 21 days, prose numbers
  nudged by a small factor, and a seeded word-substitution table
  applied consistently across the whole site. Numbers inside dates
  are never separately perturbed - a date moves whole, or not at all.
- Code inside those pages (`<code>`, `<pre>`) and code files (`.js`,
  `.css`, `.json`): numeric constants only, at a rate of one in
  fifty. Structure, tags, links, and identifiers are never touched.
- Everything else - images, fonts, archives, media - passes through
  byte-identical to everyone.

Each poisoned serving appends one JSON line to the cellar book:
timestamp, crawler, path, seed fingerprint, the alteration counts by
class, and the byte size served.

## Verification

The test suite (`python3 -m unittest discover -s tests`, 36 tests)
covers the classifier, the transformation bounds, HTML structure
preservation, determinism, and the live server end to end. A local
run against a sample page confirms the contract directly: a browser
User-Agent, a Googlebot User-Agent, and a request with no User-Agent
each receive bytes identical to the file on disk; a GPTBot
User-Agent receives the altered mirror, a second GPTBot fetch is
byte-identical to the first, and the cellar book holds exactly the
poisoned servings - two lines for two fetches, with the alteration
counts the diff shows.

## State

0.1.0 - the first release. The program, the test suite, and this
README. Not yet deployed in front of any site; deployment is an
operator decision, not a default.
