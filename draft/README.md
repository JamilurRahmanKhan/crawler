# Stage 5 — Draft

Writes the actual personalized cold email sequence: 4 steps (intro, new
angle, proof point, breakup), grounded ONLY in Stage 3's `analysis.json`
verified facts, using YOUR real offer — not a hardcoded pitch. One Gemini
call generates all 4 steps together so the sequence reads as one coherent
escalating conversation, not four disconnected blasts, per the original
design.

**Real integration, not a stub:** reuses `qa_gate.py`'s actual check
functions (`check_no_links`, `check_banned_phrases`, `check_merge_tag_leaks`,
`check_body_length`) to self-validate every draft *before* it ever reaches
the real QA gate — catching obvious violations here means fewer wasted
LLM-judge calls downstream. One retry with feedback if self-check fails;
if it still fails, the item is flagged (`_self_check_failed`) rather than
silently shipped.

## The one thing this needs that only you can provide

This project cannot know your business — same as it can't know your SMTP
password. `offer_config.json` holds it: what you sell, your ICP, your
real proof points.

```bash
cp offer_config.example.json offer_config.json
# then edit offer_config.json with your real business details
```

`draft.py` refuses to run on an unfilled config — it checks for
bracket-style placeholder text (`[YOUR COMPANY NAME]`) and required fields.

## Bootstrap style examples (optional, day-one quality before real replies exist)

Stage 10's feedback loop can only inject real winning examples *after*
real leads have replied positively — on a brand-new mailbox with zero
send history, Draft has nothing to learn from yet. `offer_config.json`
supports an optional `example_emails` key for exactly this gap:

```json
"example_emails": [
  {"subject": "quick question", "body": "Saw you just opened a new office in Austin -- always a sign of real momentum. Worth a look?"}
]
```

Hand-write 1-3 emails you'd actually be proud to send. The model matches
their *tone and structure* only — the prompt explicitly instructs it
never to reuse any fact from them for a real lead (they're a style
reference, not proof of what works, unlike Stage 10's real examples).
Delete the key entirely if you don't want it; nothing else changes.

## Judge-feedback redraft (called by qa_gate.py, not a CLI flag here)

`draft_lead()`/`build_draft_prompt()` accept a `previous_feedback: str`
parameter — a specific rejection reason from a prior attempt, woven into
the prompt as "A PREVIOUS ATTEMPT WAS REVIEWED AND REJECTED for this
reason: ... Fix this specific issue." This is what `qa_gate.py`'s
judge-feedback redraft loop (see [`qa_gate/README.md`](../qa_gate/README.md))
calls automatically when step 1 fails the judge — not something you set
by hand from this stage's own CLI.

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
# ALWAYS dry-run first -- shows what would be drafted, calls nothing, costs nothing
python draft.py --crawl-dir ../crawler/output --offer-config offer_config.json --dry-run

python draft.py --crawl-dir ../crawler/output --offer-config offer_config.json
python draft.py --crawl-dir ../crawler/output --offer-config offer_config.json --domain stripe.com
```

| Flag | Default | Meaning |
|---|---|---|
| `--crawl-dir` | `../crawler/output` | reads `contacts.json` (who) + `analysis.json` (what's true about them) per domain |
| `--offer-config` | `offer_config.json` | your real business details |
| `--queue-out` | `queue.json` | ready straight for `qa_gate.py --queue` |
| `--api-key` | `GEMINI_API_KEY` env var | |
| `--model` | `gemini-3.6-flash` | the original design used a stronger "writer" model here than for analyze/qa_gate/feedback -- not available for free (Gemini's Pro-tier carries a 0-request free quota, confirmed live), so every stage shares one flash-tier model |
| `--domain` | — | draft for a single domain only |
| `--dry-run` | off | show what would be sent, call nothing |

A domain is skipped (and counted) if Stage 4 found no usable contact, or
Stage 3 hasn't been run / didn't reach `status: ok` — never fabricates a
draft from missing upstream data.

When a domain has multiple contacts, `pick_best_contact()` selects by
final `confidence` (`high` > `medium` > `low`), not `contacts.json`'s own
list order. Real gap this fixes: `contacts.py` sorts candidates by scrape
*priority* (real page vs. guessed) before it computes each one's final
confidence, which also factors in SMTP verification — so an unverified or
guessed contact could sit ahead of one that actually verified. Ties
(equal confidence) keep the original list order, so nothing changes for
the common case of one contact, or several tied at the same confidence.

## Output → straight into qa_gate.py, zero reformatting

```bash
python draft.py --crawl-dir ../crawler/output --offer-config offer_config.json
python ../qa_gate/qa_gate.py --queue queue.json --crawl-dir ../crawler/output
```

## Verified live, not just mocked — 2 real bugs caught

1. **The placeholder-detection check would have rejected every valid
   config, including the example file itself.** `str()` on the required
   `proof_points` field (a list of dicts) produces Python repr syntax
   containing literal `[` and `{` characters — the original bare-word
   placeholder check flagged this as unfilled content on ANY config,
   valid or not. Fixed: only scan actual string leaf values, and use the
   unambiguous `[LIKE THIS]` bracket convention instead of common English
   words (a real proof point can legitimately contain a word like
   "replace" in ordinary prose).
2. **A real Draft → QA gate integration test caught a genuine
   cross-stage bug in `qa_gate.py`.** A real 4-step sequence was fed
   straight into `qa_gate.py`'s automated checks: step 1 correctly
   passed, but steps 2-4 (follow-up, proof point, breakup) were all
   incorrectly failing the grounding check — because `qa_gate.py`
   required *every* step to re-cite the same verified hook step 1 used,
   which a legitimate breakup email genuinely won't do. Fixed in
   `qa_gate.py`: grounding is now only enforced on step 1, matching the
   original sequence design. Re-verified with the same real data — all 4
   steps now correctly pass.

Also verified live against the real Gemini API with a real free-tier
key: a real call against Basecamp's actual crawled data produced 4 real
emails referencing the real verified facts, and a chained real run
(analyze → draft → qa_gate) fed those emails into a real judge call that
correctly failed all 4 for being generic relative to Basecamp's actual
profile — genuine end-to-end quality signal, not just a plumbing check.

One real bug caught this way and fixed: `gemini-3.6-flash` is a
reasoning model that spends part of `max_output_tokens` on invisible
"thinking" tokens before writing visible JSON — the original budget
(2048) hit `finish_reason=MAX_TOKENS` and silently truncated real output
mid-object; raised to 8192.

**Honest limit:** drafted email *quality* across a large, varied batch of
real leads is still not proven by one smoke test against one company, and
structurally can't be without your real `offer_config.json` filled in.
Review the first batch by hand.

## Testing

```bash
pip install pytest
python -m pytest tests/ -v
```

52 offline tests. Gemini API calls mocked at the client boundary; also
verified live against the real API (see above).
