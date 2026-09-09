"""
Stage 0/11 -- Orchestrator. Ties every stage's real batch function together
behind one command with one shared config, using the same in-process
cross-module import pattern draft.py already uses for qa_gate.py/feedback.py
(real function calls, not subprocess shelling out to each stage's CLI).

python orchestrator.py --config pipeline_config.json
"""
import json
import logging
import sys
from pathlib import Path

_BASE = Path(__file__).parent.parent
for _sibling in ["hygiene", "crawler", "analyze", "contacts", "draft", "qa_gate", "sender", "feedback"]:
    sys.path.insert(0, str(_BASE / _sibling))

import hygiene as hygiene_module  # noqa: E402 -- real cross-module reuse, not a stub
import crawl as crawl_module  # noqa: E402
import analyze as analyze_module  # noqa: E402
import contacts as contacts_module  # noqa: E402
import draft as draft_module  # noqa: E402
import qa_gate as qa_gate_module  # noqa: E402
import sender as sender_module  # noqa: E402
import feedback as feedback_module  # noqa: E402
import db as db_module  # noqa: E402 -- sender/db.py, the shared state DB
import mailboxes as mailboxes_module  # noqa: E402

log = logging.getLogger("orchestrator")

STAGE_ORDER = ["hygiene", "crawl", "analyze", "contacts", "draft", "qa_gate", "send"]


def build_work_paths(work_dir: str, crawl_dir_override: str = None) -> dict:
    work_dir = Path(work_dir)
    return {
        "hygiene_kept": str(work_dir / "hygiene_kept.csv"),
        "hygiene_dropped": str(work_dir / "hygiene_dropped.csv"),
        "crawl_dir": crawl_dir_override or str(work_dir / "crawler_output"),
        "queue": str(work_dir / "queue.json"),
        "queue_passed": str(work_dir / "queue_passed.json"),
        "review_csv": str(work_dir / "manual_review.csv"),
        "qa_state": str(work_dir / "qa_review_state.json"),
        "few_shot": str(work_dir / "few_shot.json"),
    }


def resolve_stages(all_stages: list, only: list = None, from_stage: str = None,
                    to_stage: str = None) -> list:
    def _check(name):
        if name not in all_stages:
            raise ValueError(f"unknown stage '{name}' -- valid stages: {all_stages}")

    if only:
        for name in only:
            _check(name)
        return [s for s in all_stages if s in only]

    start = 0
    end = len(all_stages)
    if from_stage:
        _check(from_stage)
        start = all_stages.index(from_stage)
    if to_stage:
        _check(to_stage)
        end = all_stages.index(to_stage) + 1
    if start >= end:
        raise ValueError(f"from_stage '{from_stage}' must come before to_stage '{to_stage}'")
    return all_stages[start:end]


def validate_stage_inputs(stage: str, config: dict, paths: dict) -> list:
    """Fail-fast, clear error before diving into a stage -- resuming a
    partial run (e.g. --only send) should not surface a raw
    FileNotFoundError from deep inside sender.py."""
    missing = []

    if stage == "hygiene":
        input_csv = config.get("input_csv")
        if not input_csv or not Path(input_csv).exists():
            missing.append(f"input_csv ({input_csv or 'not set'})")

    elif stage == "crawl":
        if not config.get("domain") and not Path(paths["hygiene_kept"]).exists():
            missing.append(f"hygiene_kept ({paths['hygiene_kept']}) -- run hygiene first, or pass --domain")

    elif stage in ("analyze", "contacts"):
        if not Path(paths["crawl_dir"]).is_dir():
            missing.append(f"crawl_dir ({paths['crawl_dir']}) -- run crawl first")

    elif stage == "draft":
        if not Path(paths["crawl_dir"]).is_dir():
            missing.append(f"crawl_dir ({paths['crawl_dir']}) -- run crawl first")
        offer_config = config.get("offer_config")
        if not offer_config or not Path(offer_config).exists():
            missing.append(f"offer_config ({offer_config or 'not set'})")

    elif stage == "qa_gate":
        if not Path(paths["queue"]).exists():
            missing.append(f"queue ({paths['queue']}) -- run draft first")

    elif stage == "send":
        if not Path(paths["queue_passed"]).exists():
            missing.append(f"queue_passed ({paths['queue_passed']}) -- run qa_gate first")

    return missing


DEFAULT_CONFIG = {
    "work_dir": "pipeline_run",
    "url_column": "website",
    "sender_db": "../sender/sender_state.db",
    "workers": 2,
    "verify_smtp": False,
    "dry_run": False,
    "with_feedback": False,
    "force": False,
    "skip_preflight": False,
    "few_shot_limit": 5,
}


def _model_kwarg(config: dict) -> dict:
    """Only pass model= through when explicitly configured, so each stage
    keeps its own DEFAULT_MODEL (draft.py deliberately uses a different
    writer model than the sonnet used for analyze/qa_gate/feedback)."""
    return {"model": config["model"]} if config.get("model") else {}


def run_feedback_microstage(config: dict, paths: dict) -> dict:
    """Runs right before draft when --with-feedback is set: classify any
    pending replies, then export the winning positive-reply examples for
    draft.py to reference -- this is the actual compounding loop, not a
    one-off script; a plain function call away from being cron'd."""
    conn = db_module.get_connection(config.get("sender_db"))
    db_module.init_db(conn)
    classify_summary = feedback_module.run_sentiment_batch(
        conn, api_key=config.get("api_key"), **_model_kwarg(config))
    examples = feedback_module.build_few_shot_examples(
        conn, str(paths["crawl_dir"]), limit=config.get("few_shot_limit", 5))
    Path(paths["few_shot"]).write_text(json.dumps(examples, indent=2, ensure_ascii=False), encoding="utf-8")
    conn.close()
    return {"classified": classify_summary.get("classified", 0), "few_shot_exported": len(examples)}


def run_pipeline(config: dict) -> dict:
    # hygiene.py creates work_dir as a side effect of writing its own
    # output CSVs there, but a resumed run (--only draft,qa_gate,send with
    # analysis.json placed by an earlier separate run) may never call
    # hygiene at all -- ensure the directory exists regardless of which
    # stage runs first, so e.g. draft.py's unconditional queue.json write
    # doesn't crash with a raw FileNotFoundError.
    Path(config["work_dir"]).mkdir(parents=True, exist_ok=True)
    paths = build_work_paths(config["work_dir"], config.get("crawl_dir"))
    stages = resolve_stages(STAGE_ORDER, only=config.get("only"),
                             from_stage=config.get("from_stage"), to_stage=config.get("to_stage"))

    results = {}
    domain_filter = {config["domain"]} if config.get("domain") else None

    for stage in stages:
        missing = validate_stage_inputs(stage, config, paths)
        if missing:
            raise SystemExit(f"Cannot run stage '{stage}': missing required input(s): {missing}")

        log.info(f"=== Stage: {stage} ===")

        if stage == "hygiene":
            results["hygiene"] = hygiene_module.run_hygiene(
                config["input_csv"], config.get("url_column", "website"),
                paths["hygiene_kept"], paths["hygiene_dropped"],
                sender_db_path=config.get("sender_db"))

        elif stage == "crawl":
            if config.get("domain"):
                domains = [config["domain"]]
            else:
                domains = crawl_module.load_domains_from_csv(paths["hygiene_kept"], "website")
            results["crawl"] = crawl_module.run_batch(
                domains, Path(paths["crawl_dir"]), config.get("workers", 2),
                force=config.get("force", False))

        elif stage == "analyze":
            # dry_run is scoped to the send stage only (a "don't actually
            # email anyone" safety valve) -- it must not also make analyze
            # skip its real Claude call, which is what analyze.py's own
            # dry_run means (log the prompt, call nothing).
            results["analyze"] = analyze_module.run_batch(
                Path(paths["crawl_dir"]), config.get("api_key"),
                domain_filter=domain_filter, **_model_kwarg(config))

        elif stage == "contacts":
            results["contacts"] = contacts_module.run_batch(
                Path(paths["crawl_dir"]), config.get("verify_smtp", False),
                domain_filter=domain_filter, opencorporates_token=config.get("opencorporates_token"))

        elif stage == "draft":
            few_shot_path = config.get("few_shot_path")
            if config.get("with_feedback"):
                results["feedback"] = run_feedback_microstage(config, paths)
                few_shot_path = paths["few_shot"]
            results["draft"] = draft_module.run_batch(
                paths["crawl_dir"], config["offer_config"], paths["queue"],
                api_key=config.get("api_key"), domain_filter=domain_filter,
                few_shot_path=few_shot_path, **_model_kwarg(config))

        elif stage == "qa_gate":
            results["qa_gate"] = qa_gate_module.run_batch(
                paths["queue"], paths["crawl_dir"], paths["queue_passed"], paths["review_csv"],
                api_key=config.get("api_key"), state_path=paths["qa_state"], **_model_kwarg(config))

        elif stage == "send":
            queue = json.loads(Path(paths["queue_passed"]).read_text(encoding="utf-8"))
            conn = db_module.get_connection(config.get("sender_db"))
            db_module.init_db(conn)
            mailbox_pool = (mailboxes_module.load_mailboxes(config["mailboxes"])
                             if config.get("mailboxes") else None)
            dry_run = config.get("dry_run", False)

            # sender.py's own CLI never skips this before a real send --
            # calling run_batch directly (as we do here) bypasses main(),
            # so the same guard has to be replicated here or a real batch
            # could go out with unvalidated creds and no SPF/DMARC check.
            if not dry_run:
                if mailbox_pool is None:
                    sender_module.Config.require_send_ready()
                else:
                    sender_module.Config.require_compliance_ready()
                sender_module.run_preflight_check(
                    mailbox_pool or [sender_module._single_mailbox_from_config()],
                    skip=config.get("skip_preflight", False))

            results["send"] = sender_module.run_batch(
                queue, conn, mailboxes=mailbox_pool,
                dry_run=dry_run, limit=config.get("limit"))
            conn.close()

    return results


def merge_config(file_config: dict, cli_overrides: dict) -> dict:
    """CLI flags override the config file; a CLI flag left at its argparse
    default of None means 'not passed' and never clobbers a file value or
    the built-in default -- required for boolean flags (--dry-run etc.),
    which must default to None in argparse, not False, to be tri-state."""
    merged = {**DEFAULT_CONFIG, **file_config}
    for key, value in cli_overrides.items():
        if value is not None:
            merged[key] = value
    return merged


def load_config_file(path: str) -> dict:
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"Config file not found: {path}")
    return json.loads(p.read_text(encoding="utf-8"))


def main():
    import argparse
    ap = argparse.ArgumentParser(
        description="Stage 0/11: Orchestrator -- runs the full cold-email pipeline "
                     "(Hygiene through Send, with an optional Feedback microstage) "
                     "as one command, wiring each stage's real output into the next "
                     "stage's real input.")
    ap.add_argument("--config", help="JSON file with pipeline settings (see pipeline_config.example.json)")
    ap.add_argument("--input-csv", default=None, help="raw leads CSV (needed for the hygiene stage)")
    ap.add_argument("--url-column", default=None)
    ap.add_argument("--work-dir", default=None, help="where every stage's intermediate files live (default: pipeline_run)")
    ap.add_argument("--crawl-dir", default=None, help="override crawl output dir (default: <work-dir>/crawler_output)")
    ap.add_argument("--offer-config", default=None, help="needed for the draft stage")
    ap.add_argument("--sender-db", default=None, help="shared sqlite state DB (default: ../sender/sender_state.db)")
    ap.add_argument("--api-key", default=None, help="Anthropic API key (default: ANTHROPIC_API_KEY env var)")
    ap.add_argument("--model", default=None, help="override the model for every LLM-calling stage "
                                                    "(default: each stage keeps its own default)")
    ap.add_argument("--workers", type=int, default=None, help="crawler concurrency")
    ap.add_argument("--domain", default=None, help="run the whole pipeline for a single domain only")
    ap.add_argument("--verify-smtp", action="store_true", default=None, help="contacts stage: probe port 25")
    ap.add_argument("--opencorporates-token", default=None)
    ap.add_argument("--mailboxes", default=None, help="send stage: JSON file of mailbox configs for rotation")
    ap.add_argument("--limit", type=int, default=None, help="send stage: max sends this invocation")
    ap.add_argument("--skip-preflight", action="store_true", default=None)
    ap.add_argument("--force", action="store_true", default=None, help="crawl stage: force re-crawl even if cached")
    ap.add_argument("--dry-run", action="store_true", default=None,
                     help="send stage only: log what would be sent, send nothing "
                          "(analyze/draft still run for real -- pass --to-stage to stop before send instead)")
    ap.add_argument("--with-feedback", action="store_true", default=None,
                     help="before draft: classify pending replies and export fresh few-shot examples")
    ap.add_argument("--few-shot-path", default=None, help="draft stage: use this few-shot file instead of --with-feedback")
    ap.add_argument("--few-shot-limit", type=int, default=None)
    ap.add_argument("--only", nargs="+", default=None, help=f"run only these stages: {STAGE_ORDER}")
    ap.add_argument("--from-stage", default=None, choices=STAGE_ORDER)
    ap.add_argument("--to-stage", default=None, choices=STAGE_ORDER)
    args = ap.parse_args()

    file_config = load_config_file(args.config)
    cli_overrides = {
        "input_csv": args.input_csv, "url_column": args.url_column, "work_dir": args.work_dir,
        "crawl_dir": args.crawl_dir, "offer_config": args.offer_config, "sender_db": args.sender_db,
        "api_key": args.api_key, "model": args.model, "workers": args.workers, "domain": args.domain,
        "verify_smtp": args.verify_smtp, "opencorporates_token": args.opencorporates_token,
        "mailboxes": args.mailboxes, "limit": args.limit, "skip_preflight": args.skip_preflight,
        "force": args.force, "dry_run": args.dry_run, "with_feedback": args.with_feedback,
        "few_shot_path": args.few_shot_path, "few_shot_limit": args.few_shot_limit,
        "only": args.only, "from_stage": args.from_stage, "to_stage": args.to_stage,
    }
    config = merge_config(file_config, cli_overrides)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    results = run_pipeline(config)

    print("\n=== Pipeline summary ===")
    for stage, summary in results.items():
        print(f"{stage}: {summary}")


if __name__ == "__main__":
    main()
