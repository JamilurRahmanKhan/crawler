# Stage 3 — Analyze

Turns crawled page text into structured business data — what a company
does, their pain points, hiring/tech signals, and concrete hooks worth
writing a cold email around. One Claude API call per domain, with a
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
a real Claude API call and costs real money per domain. You need your own
`ANTHROPIC_API_KEY`.

**Honest status on testing:** no API key was available in the session that
built this, so every function is verified with the real `anthropic` SDK
mocked at the client boundary — the plumbing (prompt building, the prefill
JSON technique, error handling, quote verification, file I/O landing in
the right place) is fully tested and was verified against **real crawled
data** (basecamp.com, buffer.com, discord.com) end-to-end with a mocked
response standing in for the actual model call. The live extraction
*quality* — whether Claude actually pulls good hooks — is not verified
here. Spot-check the first dozen or so real extractions by hand before
trusting a big batch.

## Setup

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...
```

## Run

```bash
# ALWAYS dry-run first -- shows what would be sent, calls nothing, costs nothing
python analyze.py --crawl-dir ../crawler/output --dry-run

# real run
python analyze.py --crawl-dir ../crawler/output
python analyze.py --crawl-dir ../crawler/output --domain stripe.com
python analyze.py --crawl-dir ../crawler/output --model claude-haiku-4-5-20251001  # cheaper, at 850-domain scale worth considering
```

| Flag | Default | Meaning |
|---|---|---|
| `--crawl-dir` | `../crawler/output` | where `crawl_result.json` files live |
| `--domain` | — | process just this one domain |
| `--api-key` | `ANTHROPIC_API_KEY` env var | your Claude API key |
| `--model` | `claude-sonnet-5` | which model to use |
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
  "model_used": "claude-sonnet-5",
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

`quotes_dropped`: how many claims Claude made that didn't verify against
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

30 offline tests. Anthropic API calls mocked at the client boundary for
determinism and because no key was available to test against live.
