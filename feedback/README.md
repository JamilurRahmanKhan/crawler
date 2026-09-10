# Stage 10 — Feedback loop

Closes the loop instead of sending into a void: reads real reply text
captured by `sender/reply_watcher.py`, scores sentiment, computes
per-variant metrics against thresholds from the original design, and
exports the winning positive-reply examples back into Draft (Stage 5) so
the next batch is grounded in what actually worked — not guessing fresh
every run.

## Three commands

```bash
python feedback.py --db ../sender/sender_state.db classify
python feedback.py --db ../sender/sender_state.db report
python feedback.py --db ../sender/sender_state.db export-few-shot --crawl-dir ../crawler/output --out few_shot.json
```

1. **`classify`** — every reply saved by `reply_watcher.py` but not yet
   scored gets sent to Gemini for sentiment (`positive`/`neutral`/
   `negative`, 1-10 confidence, one-line reasoning). Already-classified
   replies are skipped, so it's safe to run repeatedly (e.g. from cron).
2. **`report`** — groups sends by subject-line variant (the `Re:`
   prefixes/case/whitespace-normalized subject of the step-1 send),
   computes bounce/reply/positive rate per variant, and flags anything
   outside `DEFAULT_THRESHOLDS`. Below `MIN_SAMPLE_SIZE` (200 sent), rate
   warnings are withheld — a warning is printed that the sample is too
   small instead, not a false "healthy"/"unhealthy" verdict from noise.
3. **`export-few-shot`** — pulls every reply classified `positive`, joins
   back to the lead's domain, that domain's `analysis.json` (Stage 3) for
   the verified fact that grounded the winning email, and the subject
   that worked. Writes a JSON array ready for `draft.py --few-shot`.

## The actual compounding loop

```bash
python feedback.py --db ../sender/sender_state.db export-few-shot --crawl-dir ../crawler/output --out few_shot.json
python ../draft/draft.py --crawl-dir ../crawler/output --offer-config offer_config.json --few-shot few_shot.json
```

`draft.py`'s prompt gets a real block of past wins — company, the
verified fact it was grounded in, the subject that worked, and the
actual reply text — with an explicit instruction to draw *style* lessons
from them while still grounding each new email only in that lead's own
facts (never reusing another company's facts). No few-shot file: no
section is added, same prompt as before Stage 10 existed.

## Data captured that didn't exist before this stage

`sender/db.py`'s `replies` table (previously `reply_watcher.py` only
flipped a status flag and discarded the reply text) and a manual
`meeting_booked_at` tag on `leads` — no calendar API integration exists,
that's its own paid-tool-replacement scope; a human marks a booked
meeting after the fact.

## Verified live, not just mocked

- Full chain proven against real function calls, real SQLite, real files
  (only the Gemini sentiment call itself mocked in the unit-test suite):
  `db.mark_sent` → `db.save_reply` → `db.mark_replied` →
  `db.update_reply_sentiment` → `feedback.build_few_shot_examples` (real
  join across `leads`/`replies`/`send_log` + a real `analysis.json` on
  disk) → written to a real JSON file → `draft.build_draft_prompt`
  loading that file and injecting the block into a real prompt string.
  Verified the exported company name and grounded quote both land
  correctly in the final draft prompt.
- The Gemini sentiment call was ALSO verified live against the real API
  with a real free-tier key (`google-genai` SDK, native JSON-mode output
  -- no prefill hack needed, unlike the original Claude-based design).
  Same reasoning-model token-budget bug hit and fixed here as in every
  other stage: `gemini-3.6-flash` spends part of `max_output_tokens` on
  invisible "thinking" tokens before writing visible JSON; raised from
  256 (sized for Claude's tiny sentiment response) to 4096.
- A real bug caught before shipping: `save_reply` was storing a trailing
  newline from MIME body encoding, which would have silently broken
  exact-text matching downstream. Fixed by stripping at the storage
  boundary (`db.py`), not at every caller.
- Retries transient `429`/`503` errors (2 retries, 5s backoff), verified
  against the real exception types the SDK raises. The free tier caps out
  at **20 requests per project per day** (confirmed live elsewhere in
  this pipeline) — see [`orchestrator/README.md`](../orchestrator/README.md).

**Honest limit:** sentiment-scoring *quality* on a large, varied batch of
real replies is still not proven by a smoke test — spot-check the first
classified batch by hand.

## Setup

Uses Google's Gemini API (`google-genai` SDK) instead of the original
design's Claude, since this pipeline runs at $0 budget and Gemini has a
genuine free tier.

```bash
pip install -r requirements.txt
export GEMINI_API_KEY=AQ...
```

## Flags

| Flag | Default | Meaning |
|---|---|---|
| `--db` | `../sender/sender_state.db` | shared state DB (same one `sender.py`/`reply_watcher.py` write to) |
| `--api-key` | `GEMINI_API_KEY` env var | only needed for `classify` |
| `--model` | `gemini-3.6-flash` | |
| `export-few-shot --crawl-dir` | `../crawler/output` | for looking up each domain's `analysis.json` |
| `export-few-shot --out` | `few_shot.json` | ready for `draft.py --few-shot` |
| `export-few-shot --limit` | `5` | max examples exported |

## Testing

```bash
pip install pytest
python -m pytest tests/ -v
```

25 offline tests. Gemini API calls mocked at the client boundary; also
verified live against the real API (see above).
