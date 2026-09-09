# Free contact discovery + verification (Stage 4)

Free replacement for Apollo/Findymail-style contact enrichment. Reads the
`crawl_result.json` files `../crawler/crawl.py` already produced — does not
crawl anything itself.

## What it does

1. Takes the emails already scraped off each site (mailto links + page text,
   already junk-filtered by the crawler).
2. Ranks them for a SALES pitch: founder/owner/sales-titled addresses first,
   then a personal-looking address (`firstname.lastname@`), then generic
   `info@`/`contact@`, then `support@`/`hr@` last. Never leads with a
   support inbox.
3. **Reads real name+title data** — primarily the crawler's structural
   team-page extraction (`team_members`, from actual DOM structure) plus
   schema.org JSON-LD, with a prose-text regex scan as a supplementary
   source. This is the "who works here" data Apollo sells.
4. **Apollo's actual core trick, replicated for free — email pattern
   inference.** Apollo's email finder mostly just applies a known
   convention it learned for a domain. This does the same: the moment it
   has ONE confirmed real (name, email) pair at a domain, it derives that
   domain's convention (`{first}.{last}@`, `{f}{last}@`, etc.) and applies
   it to every other named person who has no directly-published email. When
   no pair exists to learn from, it falls back to a **bounded** brute-force
   of common conventions for the domain's most senior decision-makers only
   (capped at 5 people × 5 templates) — and only keeps a result if SMTP
   actually confirms it. Verified live: on buffer.com (zero published
   emails anywhere) this surfaced two real, SMTP-confirmed contacts —
   `carolyn@buffer.com` and `hannah.voice@buffer.com` — purely from reading
   the team page.
5. Verifies deliverability for free: MX lookup + a best-effort SMTP RCPT
   probe, with catch-all detection computed **once per domain and cached**
   (not per-candidate) — both more polite to the target mail server and
   what correctly stops brute-force guessing from trusting a catch-all
   domain's false "valid" responses.
6. Falls back to common-prefix guesses (`info@`, `sales@`, `contact@`,
   `hello@`) only when the site published literally no email at all.
7. Never outputs an address that verified as hard-invalid (550-class reject).
8. Caps 1-2 contacts per company (default 2).
9. Adds a `linkedin_lookup_url` per named contact — **not a scraper**, just
   a search-URL builder for a human to manually glance at. See "Why no
   LinkedIn automation" below.
10. **Last-resort fallback, opt-in: OpenCorporates.** When a site's crawl
    names nobody at all (the actual bottleneck vs. Apollo — see below), and
    you supply your own free OpenCorporates API token, this looks up real
    government-filed officer/director records for the company. A genuine
    sanctioned API relationship, not scraping — see "OpenCorporates
    fallback" below for setup and honest caveats.

## Setup

```bash
pip install -r requirements.txt
```

## Run

```bash
python contacts.py --crawl-dir ../crawler/output
python contacts.py --crawl-dir ../crawler/output --verify-smtp
python contacts.py --crawl-dir ../crawler/output --domain stripe.com --verify-smtp
python contacts.py --crawl-dir ../crawler/output --domains-file leads.csv --domain-column website
```

| Flag | Default | Meaning |
|---|---|---|
| `--crawl-dir` | `../crawler/output` | where crawl_result.json files live |
| `--domain` | — | process just this one domain |
| `--domains-file` / `--domain-column` | — | restrict to domains listed in a CSV |
| `--verify-smtp` | off | enable the SMTP RCPT probe (see limitations below) |
| `--max-contacts` | `2` | cap per domain |
| `--opencorporates-token` | off (or `OPENCORPORATES_API_TOKEN` env var) | last-resort officer-record lookup when a site names nobody — see "OpenCorporates fallback" below |

## Output

`<crawl-dir>/<domain>/contacts.json`, written next to that domain's
`crawl_result.json`. Also `<crawl-dir>/_contacts_summary.csv` for a batch-level
audit (domain, status, best contact, confidence, verification).

`status`: `ok`, `no_contacts_found` (crawl succeeded but zero usable
addresses even after guessing), or `no_crawl_data` (missing/failed upstream
crawl — never fabricated).

`source` per contact: `scraped` (found directly on-site), `pattern_derived`
(applied a convention learned from a confirmed pair at this domain),
`pattern_bruteforce_verified` (no convention known, but SMTP confirmed one),
`pattern_guessed` (speculative, unverifiable — catch-all domain or no MX),
`guessed` (generic prefix, zero emails found anywhere).

Each contact's `confidence`: `high` (named + verified valid), `medium`
(named + independently valid, OR a real address on-site with no negative
signal), `low` (a blind guess, or an unnamed address with no SMTP
confirmation). **`invalid`-verified addresses are dropped entirely, never
output.** Note: a name match backed by only an *inconclusive* SMTP result
(the common case — SMTP verification is unreliable, see below) still gets
`medium`, not `low` — verified live on basecamp.com, where the real
co-founder's actual address got an ambiguous SMTP response (450, greylisted)
and was previously wrongly demoted to `low` despite the name match being
solid; a blind guess is always `low` regardless of any name attached to it.

## Should you turn on `--verify-smtp`?

Yes, if your network allows outbound port 25 (check with the crawler's own
network — or just try it, failures are safe: see below). It materially
improves quality: it caught Notion's real `team@makenotion.com` mailbox as
genuinely `valid`, correctly flagged Stripe's mail server as catch-all
(downgrading confidence) rather than reporting a false "valid", and — the
big one — confirmed 2 real employee mailboxes at Buffer via brute-force even
though Buffer publishes zero emails on its site. All observed live while
building this.

**Known limitations, read before trusting this over a paid verifier:**
- Needs outbound port 25. Many home ISPs and office networks block this by
  default. When blocked, every probe returns `unverified` — never falsely
  `invalid` — so it never wrongly suppresses a good lead, it's just less
  useful than a paid tool that runs from a datacenter IP with 25 open. If
  you see nothing but `unverified` on a run, that's very likely why — try
  running this stage from a VPS instead if you want real verification.
- Some large mail providers (Microsoft 365, Google Workspace among them)
  accept everything at RCPT time and bounce later — those can register as
  falsely `valid`. This is a known industry-wide limit of SMTP-level
  verification, not specific to this script; paid verifiers hit it too,
  they just have more workarounds (IP reputation, historical bounce data).
- No cross-company database. Apollo's data comes from aggregating across
  millions of companies; this only ever sees what one company's own site
  publishes or names, PLUS (opt-in) government officer records via
  OpenCorporates when nothing else is found. If a site names zero people
  AND has no OpenCorporates record (or you haven't set up a token), there's
  nothing to derive a pattern from and this falls back to generic
  `info@`-style guesses only. This remains the hard ceiling vs. Apollo —
  a licensed, aggregated, hundreds-of-millions-of-contacts database isn't
  something a free tool structurally replicates. Expect a lower hit-rate
  on sites with a thin web presence; that's the honest trade-off of
  skipping the $49+/mo cost.
- Brute-force is deliberately bounded (5 people × 5 templates per domain,
  decision-maker titles only) so an 850-domain batch doesn't turn into
  tens of thousands of SMTP probes. It will not find every possible
  person's email — it targets who's worth targeting for a sales pitch.
- Name/title matching from prose text is regex-based, not NLP — it will
  miss oddly-phrased bios. The structural team-page + JSON-LD sources are
  far more reliable since they read actual name/title pairs rather than
  guessing from sentences. Several real issues were found and fixed live
  while building this, each with a regression test: a fake dummy name and a
  mislabeled title (see the crawler's README), a third-party person
  misattributed to the wrong company (a book blurb on Stripe's careers page
  reading "Aaron Levie / CEO at Box" — now rejected via a trailing-company
  check), and a first-person self-introduction ("I'm Jason Fried, one of
  the co-founders here" on basecamp.com's real about page) that both
  third-person patterns missed entirely — including a second pass needed
  because the live text used a typographic apostrophe ('), not the ASCII
  one the regex first checked for.

## Closing the rest of the gap: manual_gaps.py

"100% like Apollo" isn't reachable through more code — see LIMITATIONS above
for why (no cross-company database, and both realistic code-based
workarounds were tried and either partially help (OpenCorporates, gated on
your own setup) or don't hold up (search-engine fallback, confirmed
non-viable at batch scale). The only thing left that actually closes the
remaining gap is a person looking up the hard cases by hand — this script
makes that fast instead of tedious.

```bash
# 1. list every domain the automation didn't confidently solve
python manual_gaps.py export --crawl-dir ../crawler/output --out gaps.csv

# 2. a human (you, or a cheap VA) opens gaps.csv, fills in found_name /
#    found_title / found_email for whatever they can find using the
#    pre-built Google/LinkedIn search links in each row. Leave blank if
#    genuinely not findable -- never guess. verified_yn=Y only if they
#    actually confirmed the person currently works there.

# 3. merge the filled-in rows back in
python manual_gaps.py import --crawl-dir ../crawler/output --in gaps.csv
```

A domain counts as a "gap" if Stage 4 found nothing at all, or found only
`low`-confidence guesses. Imported contacts get `source: "manual_lookup"`
and `confidence: "high"` (verified_yn=Y) or `"medium"` (found but not
independently confirmed) — same `contacts.json` shape as an automated find,
so Stage 5 (drafting) and Stage 7 (sending) need zero special-casing.

**Cost model:** zero added software cost. Budget roughly 2-4 minutes per
domain for someone who knows what they're looking for — for the ~350-500
domains this tool alone won't solve out of 1000 leads, that's realistically
a day or two of one person's focused time, or a cheap VA task. That's the
actual price of getting close to Apollo-level coverage for free: your time
(or someone's), not a subscription.

## OpenCorporates fallback

For a domain whose site names literally nobody (no team page, no bios, no
JSON-LD — the actual bottleneck behind the Apollo-parity gap), pass
`--opencorporates-token` and this looks up government-filed officer/director
records instead. Real public-record data aggregated across ~140
jurisdictions, a proper sanctioned API relationship — not scraping.

**Setup:** register a free account at
[opencorporates.com/api_accounts/new](https://opencorporates.com/api_accounts/new)
(self-service, a couple minutes) and pass the token via `--opencorporates-token`
or the `OPENCORPORATES_API_TOKEN` env var. This tool cannot create the
account for you.

**Honest status: not live-verified this session.** No token was available
to test against real data — the integration is implemented and unit-tested
against OpenCorporates' documented v0.4 response schema (mocked HTTP calls),
but hasn't been confirmed against a live response. If you have a token,
spot-check it on a domain you know the real officers of before trusting it
at scale.

## Search-engine fallback — built, but NOT recommended for batch use

`search_engine_fallback_people(company_name, domain)` exists in the code
but is **deliberately not wired into the batch pipeline.** Here's why,
found through actual live testing rather than assumed:

- **DuckDuckGo's HTML endpoint actively CAPTCHA-walls automated requests**
  ("Select all squares containing a duck") — the same category of hard
  anti-bot wall this whole project already refuses to bypass elsewhere.
- **Bing initially looked viable** (clean 200 responses, real result
  snippets) — but after only a handful of automated queries in the same
  session, it degraded to serving **irrelevant results**: a query about
  Stripe's founder Patrick Collison returned snippets about SpongeBob's
  Patrick Star. Not blocked outright, just silently useless — arguably
  worse than a clean failure, since low-quality results could in principle
  feed a wrong match if they happened to land on a name+title shape.

Neither search engine reliably tolerates the repeated automated querying
this tool's actual use case needs — many domains in a batch. The function
is kept for **occasional, manual, one-domain-at-a-time use** on a specific
hard lead, with the same third-party-company rejection check as everything
else. It is not, and is not claimed to be, a scaled Apollo-parity closer —
that claim would be false given what live testing actually showed.

## Why no LinkedIn automation

Apollo has LinkedIn profile data; this deliberately does not scrape
LinkedIn. Automated scraping of LinkedIn's platform is against its Terms of
Service, and LinkedIn actively pursues legal action against scrapers (hiQ
Labs, Mantheos, and others) — it also risks the scraping IP or any linked
account getting banned. That risk isn't worth taking to avoid an Apollo
subscription. Instead, each contact with a matched name gets a
`linkedin_lookup_url` — a plain Google search URL for a human to glance at
manually (`"Jane Diaz" acme.com site:linkedin.com/in`). No automated
extraction happens; it's a convenience link, not a data source.

## Testing

```bash
pip install pytest
python -m pytest tests/ -v
```

99 offline tests, no live network — SMTP responses are mocked so results
are deterministic (a live mail server's catch-all/reject behavior isn't
something a test suite should depend on). Several tests are direct
regressions from real bugs caught while verifying against live sites
(stripe.com, buffer.com, notion.so) during development — see comments
inline for what each one is guarding against.
