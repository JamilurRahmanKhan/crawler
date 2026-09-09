# Free Firecrawl replacement

Does the same job Firecrawl does for the lead-gen pipeline, at $0:
renders JS, dismisses cookie/consent popups, extracts clean article text
(strips nav/footer/ads), finds the pages that matter, pulls on-site emails.

Hardened for running unattended against a real 850-domain lead list, not
just a demo — see "Why this can actually replace Firecrawl" below.

## Setup (one time)

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

Dev/testing only:
```bash
pip install pytest fpdf2
python -m pytest tests/ -v
```
(`fpdf2` generates a real throwaway PDF fixture for the PDF-extraction test —
not used at runtime, test-only.)

## Run

Single domain:
```bash
python crawl.py --domain example.com
```

Whole leads file:
```bash
python crawl.py --domains-file leads.csv --domain-column website --out output --workers 2
```

Debugging one stubborn site (watch it live, keep raw HTML for inspection):
```bash
python crawl.py --domain example.com --headful --save-html
```

### Flags

| Flag | Default | Meaning |
|---|---|---|
| `--domain-column` | `website` | CSV column holding the site URL |
| `--workers` | `2` | domains crawled in parallel, each with its own browser. Keep at 2-3 — more doesn't speed things up much (Chromium is the bottleneck) and raises block risk |
| `--out` | `output` | output folder |
| `--max-pages` | `8` | max pages fetched per domain |
| `--timeout-ms` | `20000` | per-page navigation timeout |
| `--cache-days` | `30` | skip re-crawling a domain within N days (only applies to `status: ok` results — failures always retry) |
| `--no-cache` | off | force re-crawl even if cached |
| `--save-html` | off | also save raw HTML per page under `output/<domain>/raw/` for audit/debugging |
| `--ignore-robots` | off | do NOT respect robots.txt (not recommended) |
| `--headful` | off | show the browser window instead of headless — for debugging one site |
| `--markdown` | off | output page text as markdown (headings/links/emphasis kept) instead of flattened plain text — matches Firecrawl's actual output format |
| `--proxy` | off (or `CRAWLER_PROXY_URL` env var) | route requests through this proxy (`http://[user:pass@]host:port`) — your own proxy, not something this tool sources |
| `--screenshot` | off | save a PNG screenshot of each page under `output/<domain>/screenshots/` |

## Output

One file per domain: `output/<domain>/crawl_result.json`

```json
{
  "domain": "example.com",
  "crawled_at": "2026-09-08T...",
  "status": "ok",
  "pages": [{"url": "...", "page_type": "about", "text": "...", "chars": 1234, "low_content": false}],
  "emails": ["sales@example.com"],
  "team_members": [{"name": "Jane Diaz", "title": "Founder"}],
  "total_chars": 41000,
  "duration_sec": 8.4,
  "error": null
}
```

`team_members` is name+title pairs found two ways: (1) structural extraction
from team/about page HTML (reads actual DOM name+title pairs, e.g.
`<h3><button>Jane Diaz</button></h3>` next to `<p>Founder</p>` — this is what
trafilatura's prose extraction alone MISSES, since it treats card/button UI
text as boilerplate and drops it), and (2) schema.org JSON-LD structured
data (`<script type="application/ld+json">`, e.g. `Organization.founders`) —
higher-precision since the site is explicitly labeling this data for search
engines, so it's read from every page including home. This feeds Stage 4's
Apollo-style email-pattern-inference (see `../contacts/README.md`).

`status` is one of:
- `ok` — usable content, safe to feed downstream
- `crawl_failed` — homepage unreachable, or nothing extractable
- `blocked` — hit a CAPTCHA/bot-detection wall; retrying won't help, route to manual lookup
- `robots_disallowed` — site's robots.txt says don't crawl it; respected, not overridden

**Downstream rule: only `status: ok` feeds the personalization draft.** Never
fabricate content for the other three — that's what turns a cold email from
"specific and credible" into "obviously fake and ignored."

After a batch run, `output/_run_summary.csv` gives one row per domain
(status, pages, chars, emails, duration, error) — the fast way to audit an
850-domain run without opening JSON files one by one. Full logs (console +
rotating file) land in `output/logs/crawl.log`.

Feed `pages[].text` into the Stage 3 extraction prompt from the automation
design. Feed `emails` into Stage 4 as a first-pass contact source (site
scrape only, no enrichment — expect lower hit-rate than paid Apollo,
that's the known trade-off of skipping it).

A page entry from a PDF link carries `"source_type": "pdf"` alongside the
usual fields — same shape otherwise, so downstream code doesn't need to
special-case it.

## Re-running

Results cache for 30 days per domain (`status: ok` only — failed/blocked
crawls always retry). Delete a domain's folder under `output/`, or pass
`--no-cache`, to force a fresh crawl.

## Why this can actually replace Firecrawl

A minimal scraper (requests + BeautifulSoup, no hardening) breaks on a big
chunk of a real lead list. This one specifically handles the failure modes
that actually show up at scale:

- **Bad/self-signed TLS certs** — very common on small local-business
  sites, which is most of a cold-email list. Tolerated (`ignore_https_errors`)
  instead of hard-failing the whole domain.
- **Basic bot-fingerprinting** — default headless Chromium is trivially
  detected (`navigator.webdriver`, etc.) and silently degraded or blocked by
  many sites. Patched (webdriver flag hidden, rotated realistic UAs, real
  `Accept-Language` header).
- **CAPTCHA / Cloudflare walls** — detected explicitly (`status: blocked`)
  instead of being mistaken for a generic failure or, worse, mistaken for
  successful-but-empty content. Retrying a block wastes time; the pipeline
  should route these to manual lookup instead.
- **Transient vs. permanent errors** — a timeout gets retried with backoff;
  a nonexistent domain does not (no point burning 3 retries on a typo'd URL).
- **Hidden navigation** — sites with a JS-router nav or a hamburger menu
  that our rendered-HTML link scan doesn't see: falls back to `sitemap.xml`
  to still find about/contact/services/blog pages. That sitemap parsing
  also tolerates non-compliant sitemaps — verified live on basecamp.com,
  whose real sitemap.xml uses bare relative paths (`/gettingreal`) instead
  of the absolute URLs the spec requires; these are resolved against the
  domain instead of leaking through as unusable bare paths.
- **JS-wall-but-technically-succeeded pages** — flagged `low_content` on
  the homepage so a near-empty "success" doesn't quietly become a garbage
  personalization source downstream.
- **Your own IP getting rate-limited mid-run** — a circuit breaker backs a
  worker off for 60s after 8 consecutive failures instead of blasting
  through the rest of its assigned domains uselessly.
- **robots.txt** — respected by default. Ethical baseline, and it also
  reduces your odds of tripping a site's anti-bot system in the first place.
- **Named people extraction (team_members)** — the single highest-value
  addition for replacing Apollo: reads real name+title pairs off team/about
  pages (structural DOM read, not prose-text guessing) plus schema.org
  JSON-LD data. Two false-positive sources were found and fixed live while
  building this: a homepage product-mockup graphic containing a fake dummy
  name ("Zenith Zen") shaped exactly like a real person, and a role-titled
  phrase ("Senior Engineer") that was briefly misclassified as a name instead
  of a title. Both are covered by regression tests now.
- **PDF pages** — a discovered link ending in `.pdf` (team-roster PDFs,
  downloadable one-pagers) gets fetched directly and text-extracted
  (`pypdf`), the same content Firecrawl would return for a PDF, instead of
  being silently skipped or failing.
- **Markdown output** (`--markdown`) — trafilatura supports it natively;
  matches Firecrawl's actual output contract instead of flattened plain text.
- **Proxy support** (`--proxy`) — Firecrawl distributes crawl requests
  across many IPs; this lets you route through your own proxy for the same
  reason. Bring your own — this doesn't source or manage proxies.
- **Screenshots** (`--screenshot`) — real PNG per page, same as a Firecrawl
  screenshot request.

## Known limits (still true vs. paid Firecrawl)

- **Sites with hard anti-bot infrastructure** (enterprise-grade Cloudflare
  challenge, PerimeterX, DataDome) still land in `status: blocked` — this
  detects and reports that cleanly, it does not attempt to solve/bypass it.
  This is a **boundary, not a gap left to close**: matching Firecrawl's paid
  bypass here means either paying for a CAPTCHA-solving service or building
  adversarial automation that crosses into ToS-violating territory. Not
  doing that, on purpose, regardless of remaining "similarity" percentage.
- No JS-rendering timeout tuning per-site beyond `--timeout-ms` globally;
  a very slow SPA may need a one-off `--headful` run to see what's slow.
- Proxy support (`--proxy`) is unit-tested for correctness (kwargs shape
  matches Playwright's documented proxy config) but not live-tested against
  a real proxy server in this session — no proxy was available to test
  against. Spot-check with your own proxy before relying on it at scale.
- Respect the target sites: keep `--workers` at 2-3. There's already a
  jittered ~0.5-0.9s delay between page fetches on the same domain built in.
