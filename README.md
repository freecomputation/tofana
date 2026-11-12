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

## State

Founding. The design above is settled; the first release - the
program, its test suite, and a local verification record - lands as
the next change to this repository.
