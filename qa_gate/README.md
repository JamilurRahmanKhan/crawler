# Stage 6 — QA gate

Validates a drafted email before it's allowed anywhere near `sender.py`.
Two tiers, per the original design:

1. **Automated** (free, deterministic, runs first) — word count, no links
   in email 1, no images/attachments, merge-tag/template leaks
   (`{{...}}`, literal `undefined`/`NAME`/`null`, AI self-disclosure
   phrases like "as an AI"), banned cliché phrases, reading grade, and —
   the real integration — **grounding**: does the draft actually reference
   a verified fact from Stage 3's `analysis.json` for this domain, not
   just generic filler. An automated failure short-circuits before ever
   spending an LLM call.
2. **LLM judge** — a second, separate Claude call scores grounding,
   specificity, peer-tone, and "would a busy founder actually reply"
   1-10. Needs grounding ≥ 8 and overall ≥ 7 to pass.

Human review sampling: the first 50 items ever processed (persisted
across runs) are flagged for 100% review during calibration; after that,
anything borderline (overall ≤ 7) is always flagged, plus an ongoing
random ~10% sample. Sampling is **informational** — logged for
calibration — it does not block an item that otherwise passed from
reaching the send queue. Only a genuine automated or judge failure does
that.

## Doesn't need Draft (Stage 5) to exist to be complete

Same pattern that let `sender.py` get built before Draft existed: this
takes the same input shape `sender.py --queue` already expects
(`{to_email, subject, body, step}`, `domain` optional — derived from
`to_email` if absent). It doesn't care whether that came from Stage 5 or
a manual batch you wrote today.

## Setup

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...
```

## Run

```bash
python qa_gate.py --queue queue.json --crawl-dir ../crawler/output
python qa_gate.py --queue queue.json --crawl-dir ../crawler/output --model claude-haiku-4-5-20251001
```

| Flag | Default | Meaning |
|---|---|---|
| `--queue` | required | JSON queue of drafted emails |
| `--crawl-dir` | `../crawler/output` | for looking up each domain's `analysis.json` |
| `--api-key` | `ANTHROPIC_API_KEY` env var | |
| `--model` | `claude-sonnet-5` | |
| `--passed-out` | `queue_passed.json` | ready straight for `sender.py --queue` |
| `--review-out` | `manual_review.csv` | every failure + every review-sampled pass, full audit trail |
| `--state-path` | `qa_review_state.json` | persists the "how many reviewed so far" counter across runs — delete it to restart the first-50-always-review calibration window |

## Output → straight into sender.py, zero reformatting

`queue_passed.json` uses the exact same keys `sender.py --queue` already
expects. Verified: an item that passed here was written back out with
`to_email`/`subject`/`body`/`step` intact, nothing extra required.

```bash
python qa_gate.py --queue queue.json --crawl-dir ../crawler/output
python ../sender/sender.py --queue queue_passed.json --dry-run
```

`manual_review.csv` carries every failure (with the specific violation or
judge feedback) and every review-sampled pass — an audit trail, not a
silent discard.

## Verified live, not just mocked

- The grounding check was tested against a **real verified quote** from
  earlier crawling work (Jason Fried's real self-introduction on
  basecamp.com's actual about page) written into a real
  `crawler/output/basecamp.com/` directory: a drafted email genuinely
  referencing that fact passed; the identical setup with a generic,
  ungrounded email correctly failed with the exact right reason. The
  check discriminates — it isn't a rubber stamp.
- The LLM judge call was tested against the **real Anthropic API
  endpoint** with a deliberately invalid key: got back a clean `401
  Unauthorized` (not a malformed-request error), confirming the request
  format the SDK builds is genuinely correct and reaches the real API —
  and the failure was handled exactly as designed (logged, routed to the
  review CSV with the real error, no crash, clean summary).
- A real design bug was caught before it ever shipped: the original test
  for the full batch pipeline would have been flaky (~10% chance of
  failing) because "sampled for human review" was conflated with
  "blocked from sending." Fixed before implementing — a review-sampled
  pass still reaches the send queue; only genuine failures are excluded.

**Honest limit:** no `ANTHROPIC_API_KEY` was available to test an actual
successful judge response — the plumbing (prompt construction, prefill
JSON parsing, error handling) is real and tested; the judge's real
scoring *quality* isn't verified here. Spot-check the first batch by
hand.

## Testing

```bash
pip install pytest
python -m pytest tests/ -v
```

50 offline tests. Anthropic API calls mocked at the client boundary.
