# Stage 1 — Hygiene

Runs on your raw leads CSV, **before** crawling. Kills what's not worth
spending a crawl on, so `crawler/` never wastes a fetch on a duplicate, a
dead domain, a parked placeholder, or a domain that already unsubscribed
from a past campaign.

## What it does

1. **Normalize** every URL to its root domain (strips `www.`, paths, and
   tracking params — `utm_*`, `fbclid`, `gclid`, etc.) — same technique
   `crawler/crawl.py` uses, so a lead resolves to the identical key at
   every stage.
2. **Dedupe** by root domain — keeps the first occurrence.
3. **MX check** — no MX record means the domain literally cannot receive
   email. Dropped before it ever reaches a crawl.
4. **Parked-domain detection** — nameserver-based (checks for known
   parking-service DNS, e.g. Sedo, Bodis, ParkingCrew). Best-effort, not
   exhaustive — a miss doesn't prove a domain isn't parked, just that its
   parking service isn't one of the common ones checked.
5. **Global suppression cross-check** — a REAL integration with `sender/`'s
   own database: a domain that unsubscribed or hard-bounced on a past
   campaign is read straight from `sender/sender_state.db` and dropped
   here, before it ever gets crawled or contacted again.
6. **Geo-tag** via ccTLD (`.ca` → CA, `.co.uk` → UK, etc.) — an honest,
   free signal for downstream CASL/GDPR compliance routing. Generic TLDs
   (`.com`, `.io`, ...) correctly report `unknown` rather than guessing.

## Setup

```bash
pip install -r requirements.txt
```

## Run

```bash
python hygiene.py leads_raw.csv --url-column website
python hygiene.py leads_raw.csv --url-column "Company Website" --sender-db ../sender/sender_state.db
```

| Flag | Default | Meaning |
|---|---|---|
| `--url-column` | `website` | column in your raw CSV holding the site URL |
| `--kept-out` | `hygiene_kept.csv` | ready-to-crawl leads |
| `--dropped-out` | `hygiene_dropped.csv` | dropped leads + reason, full audit trail |
| `--sender-db` | `../sender/sender_state.db` | path to sender/'s DB for the suppression cross-check — skipped gracefully if the file doesn't exist yet (e.g. before your first campaign) |

## Output → straight into crawler/, zero reformatting

`hygiene_kept.csv` uses `website` as its domain column — the exact default
`crawler/crawl.py --domain-column` already expects. Verified live: the
output of this stage was handed directly to `crawler/crawl.py` with no
reformatting and crawled successfully.

```bash
python hygiene.py leads_raw.csv --url-column website
python ../crawler/crawl.py --domains-file hygiene_kept.csv --domain-column website
```

`hygiene_dropped.csv` carries every original column plus `drop_reason`
(`invalid_url` / `no_mx` / `parked_domain` / `globally_suppressed`) — an
audit trail, not a silent discard. Spot-check it before a big batch.

## Verified live, not just mocked

- `has_mx_record` / `is_likely_parked` / `geo_tag_from_tld` run against
  real DNS: confirmed stripe.com has MX and isn't parked, confirmed a
  fabricated domain has none, confirmed `notion.so` genuinely lacks its
  own MX record (mail actually routes through `makenotion.com` — matches
  what Stage 4 independently found).
- The suppression cross-check was tested against a **real**
  `sender_state.db`, written by `sender/db.py`'s actual
  `mark_unsubscribed()` — not a hand-crafted schema stub. A domain marked
  unsubscribed through the real sender code was correctly caught and
  dropped here.
- The full kept-CSV → `crawler.py --domains-file` hand-off was run
  end-to-end against a live domain with zero reformatting.

## Testing

```bash
pip install pytest
python -m pytest tests/ -v
```

30 offline tests (DNS mocked for determinism) plus the live checks above,
run manually during development, not part of the automated suite (they hit
real DNS and would make CI flaky/slow).
