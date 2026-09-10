# Stage 0/11 — Orchestrator

Ties every stage together behind one command with one shared config, using
the same in-process cross-module import pattern `draft.py` already uses
for `qa_gate.py`/`feedback.py` — real function calls into each stage's
real `run_batch`/`run_hygiene`, not subprocess shelling out to each
stage's own CLI. No new integration surface: it's the same handoff files
(`hygiene_kept.csv` → `crawler_output/` → `analysis.json` → `queue.json`
→ `queue_passed.json`) every stage already produces and consumes on its
own, just sequenced automatically instead of by hand.

## One command, full pipeline

```bash
python orchestrator.py \
  --input-csv leads.csv --offer-config offer_config.json \
  --api-key $GEMINI_API_KEY --work-dir pipeline_run
```

Runs Hygiene → Crawl → Analyze → Contacts → Draft → QA gate → Send, in
that order, each stage's real output feeding the next stage's real input.
A JSON config file works the same way and is easier to keep around:

```bash
python orchestrator.py --config pipeline_config.json
```

CLI flags always override the config file (see `merge_config`). Copy
`pipeline_config.example.json`, fill in your real paths/key, and commit
nothing sensitive from it.

## Resuming a partial run

Every stage validates its own upstream artifact exists before running —
`--only send` on a fresh `--work-dir` fails fast with a clear message
(`missing required input(s): ['queue_passed (...) -- run qa_gate first']`)
instead of a raw traceback from three files deep in `sender.py`.

```bash
python orchestrator.py --config pipeline_config.json --only analyze contacts
python orchestrator.py --config pipeline_config.json --from-stage draft
python orchestrator.py --config pipeline_config.json --to-stage qa_gate
python orchestrator.py --config pipeline_config.json --domain acme.com   # one domain through every selected stage
```

## The actual compounding loop, wired in

```bash
python orchestrator.py --config pipeline_config.json --with-feedback
```

Right before drafting, this classifies any pending replies and exports
fresh positive-reply examples (Stage 10) into the same run's `few_shot.json`,
which `draft.py` then references for style — the loop Stage 10's own
README describes, now one flag instead of two manual commands run in the
right order by hand.

## qa_gate's judge-feedback redraft loop, wired in automatically

Whenever `offer_config` is set (already required for draft), qa_gate
gets the same file and can use it: if step 1 of a sequence fails the
LLM judge, qa_gate calls back into `draft.py` for one automatic redraft
using the judge's own specific feedback, before the item ever reaches
manual review. No separate flag needed — see
[`qa_gate/README.md`](../qa_gate/README.md) for the full mechanics and
its deliberately narrow scope (step-1 judge failures only).

## `--dry-run` is a send-safety valve, not a global no-op

`--dry-run` only stops the **send** stage from actually emailing anyone —
analyze and draft still run for real (real Claude calls, real files
written), so a "preview" run still produces a real, inspectable
`queue_passed.json` you can read before ever touching send. This was a
real bug caught by live testing (see below): an earlier version passed
`dry_run` straight through to analyze/draft too, which made a "let me
preview this safely" run silently skip drafting altogether, because
analyze.py's/draft.py's *own* `--dry-run` means "log the prompt, call
nothing" — a completely different concern that happened to share a name.
If you want analyze/draft to genuinely call nothing, run those stages
directly with their own `--dry-run` instead, or stop the pipeline with
`--to-stage contacts`.

The send stage also replicates sender.py's own real safety sequence
exactly (calling `run_batch()` directly bypasses sender.py's `main()`,
so this has to be redone here or a real batch could go out with
unvalidated credentials and no SPF/DMARC check): `Config.require_send_ready()`
(single mailbox) or `Config.require_compliance_ready()` (mailbox pool),
then the SPF/DMARC preflight warning, both skipped only in `--dry-run`.

## Verified live, not just mocked

- **Two real bugs found by literally running the pipeline**, not just
  green mocked unit tests:
  1. `work_dir` was never created before the first stage that needed it —
     invisible in unit tests (which always pre-seed `tmp_path`), but a
     real `--only draft,qa_gate,send` resume against a fresh work
     directory crashed with a raw `FileNotFoundError` from inside
     `draft.py`'s unconditional `queue.json` write. Fixed: `run_pipeline`
     now creates `work_dir` up front regardless of which stage runs first.
  2. `--dry-run` was originally threaded into analyze and draft too (see
     above) — caught by actually running a `--dry-run` pipeline and
     watching draft silently produce zero leads. Fixed: scoped to send only.
- **Full 5-stage real chain proven end to end** (analyze → contacts →
  draft → qa_gate → send) through the literal `orchestrator.run_pipeline()`
  entry point, against real crawled data (`basecamp.com`, from this
  project's earlier live crawling): a genuine verbatim quote pulled live
  from the real crawled page text passed analyze.py's hallucination
  clamp, fed a real `analysis.json` that draft.py referenced for real,
  produced a `queue.json` that passed qa_gate's real step-1 grounding
  check against that same real quote, and a real `send` dry-run correctly
  processed the resulting `queue_passed.json` (including correctly
  enforcing send-in-order across the 4 sequence steps). At the time, only
  the Claude HTTP call itself was replaced (no key available).
- **This pipeline now runs on Google's Gemini API instead of Anthropic's
  Claude** (`google-genai` SDK) — the project shipped at $0 budget with no
  Anthropic key, and a free-tier Gemini key became available. Re-run
  end to end for real with a genuine free-tier key (not mocked): a real
  `analyze` call extracted real facts from basecamp.com's actual crawled
  text (HTTP 200), fed a real `draft` call that wrote 4 real emails
  referencing those facts (HTTP 200), and a real `qa_gate` judge call
  scored all 4 for real and correctly **failed** every one — the judge's
  feedback named the specific mismatch each time ("tailor the pitch to
  Basecamp's self-serve, product-led model... instead of citing
  irrelevant metrics about a 40-person sales org"), which is genuine
  judgment quality, not a bug: the test `offer_config.json` was
  deliberately generic and didn't fit Basecamp's real profile, and the
  judge caught exactly that. ~12s per call (Gemini's flash models do
  internal "thinking" before answering) — real latency to plan around
  for a large batch.
- **Two real bugs found from that swap, live, not from mocked tests:**
  1. `gemini-3.6-flash` is a reasoning model — it spends part of
     `max_output_tokens` on invisible "thinking" tokens (~1800 seen live)
     *before* writing any visible JSON. The original budget (2048,
     carried over from Claude's usage) hit `finish_reason=MAX_TOKENS` and
     silently truncated real output mid-object. Fixed by raising the
     budget (8192 for analyze/draft, 4096 for the smaller qa_gate/feedback
     responses) in every LLM-calling stage.
  2. Gemini's Pro-tier models carry a **hard 0-request free quota** —
     confirmed live (`gemini-3.1-pro` returned `RESOURCE_EXHAUSTED, limit: 0`
     on the first call). The original design's writer/judge model split
     (a stronger model for drafting than for analysis/judging) isn't
     available for free; every stage now shares one flash-tier model. A
     genuine free-tier ceiling, not a design regression.
- **A mocking pitfall from the Anthropic era, worth keeping in mind**:
  `analyze.anthropic`, `draft.anthropic`, and `qa_gate.anthropic` were the
  *same* shared module object (Python caches a package once per process)
  — patching a class via three different dotted paths in one process
  patched the identical attribute three times, last one winning for all
  three, silently corrupting the other two stages' mocked responses. The
  same risk applies identically to `google.genai` now (also one shared
  cached module across all four stages) — mock each stage's own wrapper
  function (`call_gemini_extraction`/`call_gemini_draft`/`call_gemini_judge`/
  `call_gemini_sentiment`) rather than the shared `genai.Client` class
  whenever more than one LLM-calling stage runs in the same test process.
- **Retry-with-backoff added to all four LLM stages** (429/503, 2 retries,
  5s backoff) after hitting a real `503 UNAVAILABLE` from Google's side
  during this project — verified against the real exception types the
  SDK actually raises (`google.genai.errors.ServerError`/`ClientError`),
  not simulated ones. Later verified live for real against a genuine
  `429 RESOURCE_EXHAUSTED` too (see quota note below): the retry logic
  retried twice with the correct backoff, then failed cleanly with no
  crash — exactly as designed.
- **qa_gate's judge-feedback redraft loop verified live**: called
  directly against real Basecamp data with a real prior judge rejection,
  it produced a genuinely reworked, better-grounded email addressing that
  exact critique. A separate real end-to-end run correctly did *not*
  trigger it when step 1's real failure turned out to be an automated
  reading-grade violation rather than a judge failure — proof the scoping
  (step-1 judge failures only) discriminates correctly.
- **Newer flash models tested, `gemini-3.6-flash` kept as the default**:
  `gemini-3.7-flash` and `gemini-3.8-flash` both exist and both have free
  quota (confirmed live — quota is tracked separately per model, not
  shared pipeline-wide). But run against this pipeline's actual,
  larger production-sized prompts (`analyze.py`'s real extraction call
  against real crawled text, not a trivial one-line test), both hit
  repeated `503 UNAVAILABLE` / timeouts across multiple real attempts,
  while `gemini-3.6-flash` had already succeeded reliably on this exact
  workload dozens of times earlier the same session. Trivial prompts
  succeeding on the newer models doesn't mean they're production-ready
  right now — real-sized-prompt reliability is what was actually tested.
  No code change made; `--model` already lets you override per run
  (`python analyze.py --model gemini-3.8-flash ...`) to re-check this
  yourself once Google's service stabilizes for those models.

**Real constraint discovered live, affects the whole pipeline's practical
usability at $0, not any single stage:** `gemini-3.6-flash`'s free tier
caps out at **20 requests per project per day** (confirmed via a real
`429 RESOURCE_EXHAUSTED, limit: 20`) — a daily cap, not just a
per-minute one, and not something a run can retry past. Each lead's full
pass (analyze + draft + up to 4 judge calls, more if a redraft fires) can
use 6-10 requests, so a genuinely free day supports only a handful of
leads before hitting a wall that doesn't reset until Google's next daily
cycle. Plan real batch sizes around this, or get a quota increase /
paid tier from Google for real volume — this project's $0 design
assumption was "free tier exists," not "free tier is large."

**Honest limit:** a real key now exists, so the plumbing AND at least one
real output per stage have been proven live — but real scoring/drafting
*quality* across a large, varied batch is still not proven by a handful of
smoke-test calls against one company, and the 20/day quota makes even
gathering that evidence slow. Spot-check the first real batch by hand
regardless.

## Config keys

| Key | Default | Meaning |
|---|---|---|
| `input_csv` | — | raw leads CSV (hygiene stage) |
| `url_column` | `website` | |
| `work_dir` | `pipeline_run` | every stage's intermediate files |
| `crawl_dir` | `<work_dir>/crawler_output` | override to point at an existing crawl |
| `offer_config` | — | required for draft |
| `sender_db` | `../sender/sender_state.db` | shared state DB |
| `api_key` | `GEMINI_API_KEY` env var | |
| `model` | each stage's own default | override applies to every LLM-calling stage at once |
| `workers` | `2` | crawler concurrency |
| `domain` | — | run only this one domain through every selected stage |
| `verify_smtp` | `False` | contacts: probe port 25 |
| `opencorporates_token` | — | contacts fallback (your own account) |
| `mailboxes` | — | send: JSON file for mailbox rotation |
| `limit` | — | send: max sends this invocation |
| `skip_preflight` | `False` | send: skip the SPF/DMARC warning check |
| `force` | `False` | crawl: force re-crawl even if cached |
| `dry_run` | `False` | send-only safety valve (see above) |
| `with_feedback` | `False` | classify + export few-shot before draft |
| `few_shot_path` | — | use an existing few-shot file instead of `--with-feedback` |
| `few_shot_limit` | `5` | |
| `only` / `from_stage` / `to_stage` | full pipeline | stage selection |

## Testing

```bash
pip install pytest
python -m pytest tests/ -v
```

39 offline tests: pure-function coverage (path building, stage
resolution, config merging, input validation) plus `run_pipeline`
wiring with every stage module mocked at the call boundary. The 5-stage
live proof above is a standalone script, not part of the pytest suite
(it needs real crawled data on disk and mutates real files) — see this
README's "Verified live" section for what it covered.
