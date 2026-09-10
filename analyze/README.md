# Stage 3 — Analyze

Turns crawled page text into structured business data — what a company
does, their pain points, hiring/tech signals, and concrete hooks worth
writing a cold email around. One Gemini API call per domain, with a
**deterministic hallucination clamp**: every claim in `recent_events`,
`pains`, and `hooks` must carry a verbatim quote + source URL, and this
code verifies that quote actually appears in the crawled text before
trusting it. Fails verification → dropped, never passed downstream.

Reads `crawler/output/<domain>/crawl_result.json`. Writes
`crawler/output/<domain>/analysis.json` — same directory `contacts.json`
already lives in, so nothing downstream needs to know these came from
different stages.

## The one thing this needs that the other 3 stages didn't: an API key

Unlike hygiene/crawler/contacts/sender (all free/local), this stage makes
a real Gemini API call per domain. Uses Google's Gemini API
(`google-genai` SDK) instead of the original design's Claude, since this
pipeline runs at $0 budget and Gemini has a genuine free tier. You need
your own `GEMINI_API_KEY` (aistudio.google.com).

**Status on testing:** every function is verified with the real
`google-genai` client mocked at the client boundary — the plumbing (prompt
building, JSON-mode output, error handling, quote verification, file I/O
landing in the right place) is fully tested. It was ALSO verified live
against the real Gemini API with a real free-tier key: a real call
against basecamp.com's actual crawled text returned a genuine extraction
(`company_name: "Basecamp"`, real hooks with real quotes and source URLs
pulled from the actual page). One real bug was caught this way and fixed:
`gemini-3.6-flash` is a reasoning model that spends part of
`max_output_tokens` on invisible "thinking" tokens before writing visible
JSON — the original budget (2048) hit `finish_reason=MAX_TOKENS` and
silently truncated real output mid-object; raised to 8192. Real extraction
*quality* across a large, varied batch is still not proven by one smoke
test — spot-check the first dozen or so real extractions by hand before
trusting a big batch.

Retries transient `429`/`503` errors (2 retries, 5s backoff) — verified
against the real exception types `google.genai.errors` actually raises,
and later confirmed live against a genuine `429 RESOURCE_EXHAUSTED`: the
free tier caps out at **20 requests per project per day** (not just
per-minute), a real constraint worth planning batch sizes around — see
[`orchestrator/README.md`](../orchestrator/README.md) for the full note.

## Setup

```bash
pip install -r requirements.txt
export GEMINI_API_KEY=AQ...
```

## Run

```bash
# ALWAYS dry-run first -- shows what would be sent, calls nothing, costs nothing
python analyze.py --crawl-dir ../crawler/output --dry-run

# real run
python analyze.py --crawl-dir ../crawler/output
python analyze.py --crawl-dir ../crawler/output --domain stripe.com
python analyze.py --crawl-dir ../crawler/output --model gemini-3.6-flash-lite  # cheaper, at 850-domain scale worth considering
```

| Flag | Default | Meaning |
|---|---|---|
| `--crawl-dir` | `../crawler/output` | where `crawl_result.json` files live |
| `--domain` | — | process just this one domain |
| `--api-key` | `GEMINI_API_KEY` env var | your Gemini API key |
| `--model` | `gemini-3.6-flash` | which model to use (Gemini free tier: flash/lite models only -- Pro-tier carries a 0-request free quota, confirmed live) |
| `--dry-run` | off | show what would be sent (with real char counts from real crawled data), call nothing |

`--dry-run` correctly skips (doesn't preview) any domain whose upstream
crawl status isn't `ok` — verified live: caught and fixed a real bug where
dry-run built a misleading prompt preview for `nowsecure.nl` (a domain
that actually crawled with `status: crawl_failed`), which a real run
would have correctly skipped. Dry-run now matches reality exactly.

## Output

```json
{
  "domain": "acme.com",
  "analyzed_at": "...",
  "model_used": "gemini-3.6-flash",
  "status": "ok",
  "signal_gate": "ok",
  "company_name": "Acme", "what_they_do": "...", "icp_they_serve": "...",
  "services": [], "geo": "", "size_estimate": "",
  "recent_events": [{"event": "", "quote": "", "source_url": ""}],
  "hiring_signals": [], "tech_signals": [],
  "pains": [{"pain": "", "quote": "", "source_url": "", "confidence": 0}],
  "hooks": [{"hook": "", "quote": "", "source_url": "", "specificity": 0}],
  "disqualifiers": [],
  "quotes_dropped": 0
}
```

`status`: `ok`, `no_crawl_data` (upstream crawl wasn't `ok` — never
fabricated), `api_error`, or `parse_error` (model didn't return valid
JSON, logged, batch continues).

`signal_gate`: `"ok"` if at least one hook scores specificity ≥ 7 and
there are no disqualifiers; `"low_signal"` otherwise. Route `low_signal`
leads to a shorter, claim-free template downstream (Stage 5) instead of
forcing a personalized angle that genuinely isn't there — never invent
specificity that wasn't earned.

`quotes_dropped`: how many claims Gemini made that didn't verify against
the actual crawled text and got removed. A domain with a high count here
is worth a manual look — the model may have been reaching for something
that wasn't in the source.

## The hallucination clamp, in detail

`normalize_quote()` collapses whitespace, lowercases, and normalizes
typographic vs. straight apostrophes before comparing — the same real bug
class hit twice already in this project (Stage 2's team-name extraction,
Stage 4's self-intro regex) applied proactively here. Verified live: a
quote using a straight apostrophe (`I'm`, how a model is likely to echo
text back) correctly matched real crawled source text using a typographic
one (`I’m`, how basecamp.com's actual page renders it) — and a genuinely
fabricated claim was still correctly rejected against the same real page.

## Testing

```bash
pip install pytest
python -m pytest tests/ -v
```

33 offline tests. Gemini API calls mocked at the client boundary for
determinism; also verified live against the real API (see above).
