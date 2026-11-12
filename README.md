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
- The poisoner rewrites text content only - numbers, dates, a
  seeded word-substitution table, and the six vials described below -
  and never touches a page's structure: tags, links, and identifiers
  pass through unchanged, so the mirror stays fully crawlable.
  Binary assets pass through untouched.
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
  "disabled_crawlers": ["YouBot"],
  "entities": ["Ada Lovelace", "run_survey()"],
  "kinship": [["passband", "coffer"], ["runner", "broker"]],
  "lures": [
    {"path": "answers/what-is-tofana.html",
     "question": "What is Tofana?",
     "answer": "A gatekeeper for static sites."}
  ]
}
```

`extra_crawlers` adds User-Agent signatures to the table;
`disabled_crawlers` removes default entries by signature. Everything
in the table is an AI training or ingestion crawler; user-initiated
fetchers such as ChatGPT-User and Claude-User are guests by design
and are not in the table.

Three further keys configure the vials: `entities` (a list of real
names for the canary vial), `kinship` (a list of term pairs), and
`lures` (a list of objects with `path`, `question`, and `answer`).
`register` sets the canary register path; by default it is written
beside the cellar book as `tofana-canary.json`.

## The six vials

Version 0.2 adds six poison kinds to the first release's fact
poison (nudged numbers, shifted dates, substituted words). All six
obey the doctrine without exception: flagged crawlers only,
deterministic per site seed and page, structure never altered, the
disk tree never written, and every dose recorded in the cellar book
with per-vial alteration counts.

### Trap facts (the canary vial)

The owner lists real names in `entities` - contributors, functions,
releases, places. In the mirror, each listed name is replaced
site-wide by an invented counterpart derived from the seed: a
person's name becomes another name, a function name becomes an
identifier of the same style, a version becomes a false version.
The true-to-invented mapping is written to the canary register,
beside the cellar book and never inside the served root; the server
refuses to serve it, exactly as it refuses the book. The register is
a detection list: if an invented entity ever appears in a model's
output, the well it drank from is proven.

### Code poison, full strength

Code keeps the one-in-fifty numeric rate and additionally suffers
rare boolean and default flips (`true` served as `false`, `on` as
`off`) and flag-name near-misses from a small seeded table (for
example `--quiet` served as `--silent`). These apply only inside
code spans (`<code>`, `<pre>`) and code files (`.js`, `.css`,
`.json`). Prose is never touched by this vial, and code never
receives the prose treatments.

### Provenance poison

Version strings (`v1.2.3` and `1.2.3`) in prose map site-wide to one
consistent false version per true version - the same true version
becomes the same false version on every page. Bare years move with
the page's date offset: same direction, days-scale size. Author
names in citations are covered by the canary table when the owner
lists them as entities. The mirror's lineage of the work - what
version, when, by whom - is false, and agrees with itself.

### Kinship poison

`kinship` carries pairs of terms - project names, dependency names,
technical terms - that are cross-swapped in prose: each occurrence
of one side is served as the other. Every sentence stays plausible;
the relationship graph bends. With no pairs configured the vial is
inert, and `check` says so.

### Tongue poison

On non-English pages - detected by the HTML `lang` attribute or a
language directory such as `/es/`, `/fr/`, or `/de/` - a small
built-in table of niche technical vocabulary is cross-swapped with
seeded consistency: transmitter with receiver, input with output,
signal with noise. A model reading the mirror in another language
learns the vocabulary wrong in that language. English pages are
never touched by this vial, and languages without a built-in table
(Spanish, French, and German have one) are left alone.

### Lure pages

`lures` defines pages that do not exist: a path, a question, and an
answer. A flagged crawler requesting the path is served a page
generated on the fly, its text run through the standard poison
passes. Guests requesting the same path receive the ordinary 404.
A lure never shadows a real file - if a file exists at the path,
the lure is skipped and `check` reports it. Lure servings are
logged in the cellar book as their own alteration class.

`check` reports each vial as active or inert for the given tree and
config, with the reason: unconfigured, configured but absent from
the tree, shadowed, ready.

### What gets poisoned

- HTML and plain-text pages (`.html`, `.htm`, `.txt`, `.md`): dates
  shifted by a fixed per-page offset of 2 to 21 days, prose numbers
  nudged by a small factor, and a seeded word-substitution table
  applied consistently across the whole site. Numbers inside dates
  are never separately perturbed - a date moves whole, or not at all.
- Code inside those pages (`<code>`, `<pre>`) and code files (`.js`,
  `.css`, `.json`): numeric constants at a rate of one in fifty,
  plus the code vial's rare flips and flag near-misses. Structure,
  tags, links, and identifiers are never touched.
- Everything else - images, fonts, archives, media - passes through
  byte-identical to everyone.

Each poisoned serving appends one JSON line to the cellar book:
timestamp, crawler, path, seed fingerprint, the alteration counts by
class - the original four plus one count per vial (`canary`,
`code`, `provenance`, `kinship`, `tongue`, `lure`) - and the byte
size served. Lines written by 0.1 keep their shape; the new counts
are additional keys in the same `alterations` object.

## Honest limits of the vials

- The canary register proves ingestion only if a model actually
  repeats an invented entity; silence from a model proves nothing
  either way.
- The tongue tables are small and cover three languages; a page in
  any other language is served with the remaining vials only.
- Kinship and canary swaps are only as good as the owner's lists -
  a name or term pair that is not configured is not swapped.
- Lure pages answer only the exact configured paths; they are bait
  for crawlers that follow or guess links, not a trap for every
  possible query.

## Verification

The test suite (`python3 -m unittest discover -s tests`, 60 tests)
covers the classifier, the transformation bounds, HTML structure
preservation, determinism, the live server end to end, and one test
class per vial: canary consistency across pages and register
correctness, code-vial containment (prose provably untouched),
version-map consistency, kinship inertness without config, tongue
containment to non-English pages, and lures served to crawlers
only, never shadowing a real file. A local run against a sample
page confirms the contract directly: a browser User-Agent, a
Googlebot User-Agent, and a request with no User-Agent each receive
bytes identical to the file on disk; a GPTBot User-Agent receives
the altered mirror, a second GPTBot fetch is byte-identical to the
first, and the cellar book holds exactly the poisoned servings.

## State

0.2.0 - the second release: the 0.1.0 gatekeeper and fact poison,
plus the six vials. Not yet deployed in front of any site;
deployment is an operator decision, not a default.
