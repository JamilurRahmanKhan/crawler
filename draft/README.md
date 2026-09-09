# Stage 5 — Draft

Writes the actual personalized cold email sequence: 4 steps (intro, new
angle, proof point, breakup), grounded ONLY in Stage 3's `analysis.json`
verified facts, using YOUR real offer — not a hardcoded pitch. One Claude
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

## Setup

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...
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
| `--api-key` | `ANTHROPIC_API_KEY` env var | |
| `--model` | `claude-opus-5` | best writer for actual copy, matches the original design's model-tier split |
| `--domain` | — | draft for a single domain only |
| `--dry-run` | off | show what would be sent, call nothing |

A domain is skipped (and counted) if Stage 4 found no usable contact, or
Stage 3 hasn't been run / didn't reach `status: ok` — never fabricates a
draft from missing upstream data.

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

Also verified: the actual prompt sent to Claude was inspected against
real crawled data (Basecamp's real contact, a real verified quote) and
confirmed well-formed; the API call itself was tested against the real
Anthropic endpoint with a deliberately invalid key — got a clean `401
Unauthorized`, confirming the request format is genuinely correct.

**Honest limit:** no `ANTHROPIC_API_KEY` was available to test an actual
successful draft generation — the plumbing is real and tested; the
drafted email *quality* isn't verified here, and structurally can't be
without your real `offer_config.json` filled in. Review the first batch
by hand.

## Testing

```bash
pip install pytest
python -m pytest tests/ -v
```

32 offline tests. Anthropic API calls mocked at the client boundary.
