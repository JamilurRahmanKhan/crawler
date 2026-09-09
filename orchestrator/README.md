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
  --api-key $ANTHROPIC_API_KEY --work-dir pipeline_run
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
  enforcing send-in-order across the 4 sequence steps). Only the Claude
  HTTP call itself was replaced (no `ANTHROPIC_API_KEY` in this
  environment) — everything else (file I/O, hallucination-clamp
  verification, self-check, grounding check, contact parsing, DB writes)
  is the real code path.
- **A real mocking pitfall surfaced by chaining three LLM-calling stages
  in one process for the first time**: `analyze.anthropic`, `draft.anthropic`,
  and `qa_gate.anthropic` are the *same* shared module object (Python
  caches the `anthropic` package once per process) — patching
  `Anthropic` via three different dotted paths in one test patches the
  identical attribute three times, and the last one wins for all three,
  silently corrupting the other two stages' mocked responses. This is
  invisible in each stage's own isolated test suite (one stage's Claude
  call per test process) and doesn't affect real usage (each real call
  is independent, scoped by its own arguments) — it only bites a test
  harness mocking multiple stages' Claude calls together. Worth knowing
  if you extend this proof: mock each stage's own wrapper function
  (`call_claude_extraction`/`call_claude_draft`/`call_claude_judge`)
  instead of the shared `anthropic.Anthropic` class when more than one
  LLM-calling stage runs in the same process.

**Honest limit:** no `ANTHROPIC_API_KEY` was available to prove the real
end-to-end chain with genuine Claude responses — the plumbing is real and
proven; each stage's own README already documents its individual
live-401-endpoint check and honest scoring-quality limits.

## Config keys

| Key | Default | Meaning |
|---|---|---|
| `input_csv` | — | raw leads CSV (hygiene stage) |
| `url_column` | `website` | |
| `work_dir` | `pipeline_run` | every stage's intermediate files |
| `crawl_dir` | `<work_dir>/crawler_output` | override to point at an existing crawl |
| `offer_config` | — | required for draft |
| `sender_db` | `../sender/sender_state.db` | shared state DB |
| `api_key` | `ANTHROPIC_API_KEY` env var | |
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

38 offline tests: pure-function coverage (path building, stage
resolution, config merging, input validation) plus `run_pipeline`
wiring with every stage module mocked at the call boundary. The 5-stage
live proof above is a standalone script, not part of the pytest suite
(it needs real crawled data on disk and mutates real files) — see this
README's "Verified live" section for what it covered.
