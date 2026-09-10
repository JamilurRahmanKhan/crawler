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
2. **LLM judge** — a second, separate Gemini call scores grounding,
   specificity, peer-tone, and "would a busy founder actually reply"
   1-10. Needs grounding ≥ 8 and overall ≥ 7 to pass.

Human review sampling: the first 50 items ever processed (persisted
across runs) are flagged for 100% review during calibration; after that,
anything borderline (overall ≤ 7) is always flagged, plus an ongoing
random ~10% sample. Sampling is **informational** — logged for
calibration — it does not block an item that otherwise passed from
reaching the send queue. Only a genuine automated or judge failure does
that.

## Judge-feedback redraft loop (opt-in via `--offer-config`)

When step 1 of a sequence fails the **judge** specifically (not an
automated check), the judge's own one-sentence feedback gets fed back to
`draft.py` for one automatic redraft of the whole 4-step sequence — not
a single step in isolation, since Draft writes all 4 as one coherent
thread and redrafting one step alone would break steps 2-4's continuity.
The redrafted sequence is re-checked exactly like any other; if it now
passes, it goes to the send queue; if not, it's what reaches manual
review (only ever one redraft attempt, never a loop that could burn
API quota chasing a perfect score).

```bash
python qa_gate.py --queue queue.json --crawl-dir ../crawler/output --offer-config ../draft/offer_config.json
```

Omit `--offer-config` and this is fully disabled — zero behavior change
from before this existed. `draft.py` is imported **lazily** (inside the
function that needs it, not at module load time) specifically so this
stage keeps working standalone even in an environment where `draft.py`
isn't present, preserving the "doesn't need Draft to exist to be
complete" property stated above.

**Scope, deliberately narrow:** only triggers on a step-1 **judge**
failure. An automated-check failure (banned phrase, reading grade, a
link in email 1) is deterministic and already tells you exactly what's
wrong — redrafting won't fix "reading grade too high" without also
changing the redraft prompt to say so, which isn't wired up (a real
possible future extension, not built). A step 2/3/4 judge failure alone
also doesn't trigger a redraft, to avoid churning the whole sequence over
one weaker follow-up email.

## Doesn't need Draft (Stage 5) to exist to be complete

Same pattern that let `sender.py` get built before Draft existed: this
takes the same input shape `sender.py --queue` already expects
(`{to_email, subject, body, step}`, `domain` optional — derived from
`to_email` if absent). It doesn't care whether that came from Stage 5 or
a manual batch you wrote today.

## Setup

Uses Google's Gemini API (`google-genai` SDK) instead of the original
design's Claude, since this pipeline runs at $0 budget and Gemini has a
genuine free tier.

```bash
pip install -r requirements.txt
export GEMINI_API_KEY=AQ...
```

## Run

```bash
python qa_gate.py --queue queue.json --crawl-dir ../crawler/output
python qa_gate.py --queue queue.json --crawl-dir ../crawler/output --model gemini-3.6-flash-lite
```

| Flag | Default | Meaning |
|---|---|---|
| `--queue` | required | JSON queue of drafted emails |
| `--crawl-dir` | `../crawler/output` | for looking up each domain's `analysis.json` |
| `--api-key` | `GEMINI_API_KEY` env var | |
| `--model` | `gemini-3.6-flash` | |
| `--passed-out` | `queue_passed.json` | ready straight for `sender.py --queue` |
| `--review-out` | `manual_review.csv` | every failure + every review-sampled pass, full audit trail |
| `--state-path` | `qa_review_state.json` | persists the "how many reviewed so far" counter across runs — delete it to restart the first-50-always-review calibration window |
| `--offer-config` | none (disabled) | enables the judge-feedback redraft loop above |

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
- A real design bug was caught before it ever shipped: the original test
  for the full batch pipeline would have been flaky (~10% chance of
  failing) because "sampled for human review" was conflated with
  "blocked from sending." Fixed before implementing — a review-sampled
  pass still reaches the send queue; only genuine failures are excluded.
- The LLM judge call has since been verified live against the real
  Gemini API with a real free-tier key, chained through a real
  analyze → draft → qa_gate run: a real judge call scored 4 real drafted
  emails and correctly **failed** all 4 for being generic relative to
  Basecamp's actual real profile, naming the specific mismatch each time
  ("tailor the pitch to Basecamp's self-serve, product-led model...
  instead of citing irrelevant metrics about a 40-person sales org") —
  genuine judgment quality, not a rubber stamp, and not just a plumbing
  check. One real bug caught this way and fixed: `gemini-3.6-flash` is a
  reasoning model that spends part of `max_output_tokens` on invisible
  "thinking" tokens before writing visible JSON — the original budget
  (512, sized for Claude's tiny judge response) hit
  `finish_reason=MAX_TOKENS` and would have silently truncated real
  output; raised to 4096.
- **The redraft loop was verified live**, real call, no mocks:
  `redraft_sequence_with_feedback()` called directly against real
  Basecamp data with a real prior judge rejection ("Replace the
  passive-aggressive breakup template with a specific value proposition
  referencing Basecamp's actual product strategy") produced a genuinely
  reworked, better-grounded step-1 email addressing that exact critique.
  A separate real end-to-end `run_batch` run correctly did **not**
  trigger the redraft when step 1's actual failure turned out to be an
  automated reading-grade violation rather than a judge failure — proof
  the scoping condition (step-1 judge failure specifically) discriminates
  correctly rather than firing on any failure.

**Real constraint discovered live, affects the whole pipeline, not just
this stage:** `gemini-3.6-flash`'s free tier caps out at **20 requests
per project per day** (confirmed via a real `429 RESOURCE_EXHAUSTED,
limit: 20`), not just a per-minute rate limit. Each lead's full pass
through analyze + draft + judge (×4 steps, plus a possible redraft's own
+4 judge calls) can easily use 6-10 requests — meaning a genuinely free
run supports only a handful of leads per day before hitting a wall that
resets on Google's daily cycle, not something retryable within the same
run. Plan real batches accordingly, or request a quota increase /
paid tier from Google for real volume.

**Honest limit:** the judge's real scoring quality has now been spot-
verified a few times, live, on one real company's drafts — that's a
genuine signal but not a large-batch guarantee. Spot-check the first
real batch by hand regardless.

## Testing

```bash
pip install pytest
python -m pytest tests/ -v
```

63 offline tests. Gemini API calls mocked at the client boundary; also
verified live against the real API (see above).
