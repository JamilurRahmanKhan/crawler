import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
import orchestrator
from orchestrator import (
    STAGE_ORDER,
    build_work_paths,
    resolve_stages,
    validate_stage_inputs,
    merge_config,
    run_pipeline,
    load_config_file,
)


def _seed_all_stage_artifacts(tmp_path, paths):
    """Pre-create every stage's expected upstream artifact so
    validate_stage_inputs never blocks the mocked run -- this test is
    about call wiring/order, not real file production (that's covered by
    the live end-to-end integration proof, not a unit test)."""
    Path(paths["hygiene_kept"]).parent.mkdir(parents=True, exist_ok=True)
    input_csv = tmp_path / "leads.csv"
    input_csv.write_text("website\nacme.com\n", encoding="utf-8")
    Path(paths["hygiene_kept"]).write_text("website\nacme.com\n", encoding="utf-8")
    Path(paths["crawl_dir"]).mkdir(parents=True, exist_ok=True)
    Path(paths["queue"]).write_text("[]", encoding="utf-8")
    Path(paths["queue_passed"]).write_text("[]", encoding="utf-8")
    offer_config = tmp_path / "offer.json"
    offer_config.write_text("{}", encoding="utf-8")
    return str(input_csv), str(offer_config)


# --- build_work_paths --------------------------------------------------------

def test_build_work_paths_returns_all_expected_keys():
    paths = build_work_paths("run1")
    for key in ["hygiene_kept", "hygiene_dropped", "crawl_dir", "queue",
                "queue_passed", "review_csv", "qa_state", "few_shot"]:
        assert key in paths


def test_build_work_paths_nests_under_work_dir():
    paths = build_work_paths("run1")
    assert paths["queue"] == str(Path("run1") / "queue.json")
    assert paths["crawl_dir"] == str(Path("run1") / "crawler_output")


def test_build_work_paths_honors_crawl_dir_override():
    paths = build_work_paths("run1", crawl_dir_override="shared/crawler_output")
    assert paths["crawl_dir"] == "shared/crawler_output"
    # everything else still nests under work_dir
    assert paths["queue"] == str(Path("run1") / "queue.json")


# --- resolve_stages -----------------------------------------------------------

def test_resolve_stages_defaults_to_full_order():
    assert resolve_stages(STAGE_ORDER) == STAGE_ORDER


def test_resolve_stages_only_filters_and_preserves_pipeline_order():
    result = resolve_stages(STAGE_ORDER, only=["send", "hygiene"])
    assert result == ["hygiene", "send"]


def test_resolve_stages_from_stage_runs_to_the_end():
    result = resolve_stages(STAGE_ORDER, from_stage="contacts")
    assert result == ["contacts", "draft", "qa_gate", "send"]


def test_resolve_stages_to_stage_stops_early():
    result = resolve_stages(STAGE_ORDER, to_stage="analyze")
    assert result == ["hygiene", "crawl", "analyze"]


def test_resolve_stages_from_and_to_combine():
    result = resolve_stages(STAGE_ORDER, from_stage="crawl", to_stage="contacts")
    assert result == ["crawl", "analyze", "contacts"]


def test_resolve_stages_unknown_only_name_raises():
    with pytest.raises(ValueError, match="unknown stage"):
        resolve_stages(STAGE_ORDER, only=["smtp_send"])


def test_resolve_stages_unknown_from_stage_raises():
    with pytest.raises(ValueError, match="unknown stage"):
        resolve_stages(STAGE_ORDER, from_stage="nope")


def test_resolve_stages_from_after_to_raises():
    with pytest.raises(ValueError, match="before"):
        resolve_stages(STAGE_ORDER, from_stage="send", to_stage="hygiene")


# --- validate_stage_inputs ------------------------------------------------------

def test_validate_hygiene_requires_input_csv(tmp_path):
    paths = build_work_paths(str(tmp_path))
    missing = validate_stage_inputs("hygiene", {}, paths)
    assert missing

    csv_path = tmp_path / "leads.csv"
    csv_path.write_text("website\nacme.com\n", encoding="utf-8")
    missing = validate_stage_inputs("hygiene", {"input_csv": str(csv_path)}, paths)
    assert missing == []


def test_validate_crawl_requires_hygiene_kept_unless_single_domain(tmp_path):
    paths = build_work_paths(str(tmp_path))
    missing = validate_stage_inputs("crawl", {}, paths)
    assert missing

    missing = validate_stage_inputs("crawl", {"domain": "acme.com"}, paths)
    assert missing == []

    Path(paths["hygiene_kept"]).write_text("website\nacme.com\n", encoding="utf-8")
    missing = validate_stage_inputs("crawl", {}, paths)
    assert missing == []


def test_validate_analyze_and_contacts_require_crawl_dir(tmp_path):
    paths = build_work_paths(str(tmp_path))
    assert validate_stage_inputs("analyze", {}, paths)
    assert validate_stage_inputs("contacts", {}, paths)

    Path(paths["crawl_dir"]).mkdir(parents=True)
    assert validate_stage_inputs("analyze", {}, paths) == []
    assert validate_stage_inputs("contacts", {}, paths) == []


def test_validate_draft_requires_crawl_dir_and_offer_config(tmp_path):
    paths = build_work_paths(str(tmp_path))
    Path(paths["crawl_dir"]).mkdir(parents=True)
    missing = validate_stage_inputs("draft", {}, paths)
    assert missing

    offer_path = tmp_path / "offer.json"
    offer_path.write_text("{}", encoding="utf-8")
    missing = validate_stage_inputs("draft", {"offer_config": str(offer_path)}, paths)
    assert missing == []


def test_validate_qa_gate_requires_queue(tmp_path):
    paths = build_work_paths(str(tmp_path))
    assert validate_stage_inputs("qa_gate", {}, paths)

    Path(paths["queue"]).write_text("[]", encoding="utf-8")
    assert validate_stage_inputs("qa_gate", {}, paths) == []


def test_validate_send_requires_queue_passed(tmp_path):
    paths = build_work_paths(str(tmp_path))
    assert validate_stage_inputs("send", {}, paths)

    Path(paths["queue_passed"]).write_text("[]", encoding="utf-8")
    assert validate_stage_inputs("send", {}, paths) == []


# --- merge_config ---------------------------------------------------------------

def test_merge_config_fills_defaults_when_nothing_given():
    merged = merge_config({}, {})
    assert merged["work_dir"] == "pipeline_run"
    assert merged["url_column"] == "website"
    assert merged["dry_run"] is False
    assert merged["with_feedback"] is False


def test_merge_config_file_value_overrides_default():
    merged = merge_config({"work_dir": "custom_run"}, {})
    assert merged["work_dir"] == "custom_run"


def test_merge_config_cli_override_wins_over_file():
    merged = merge_config({"work_dir": "from_file"}, {"work_dir": "from_cli"})
    assert merged["work_dir"] == "from_cli"


def test_merge_config_cli_none_does_not_clobber_file_value():
    merged = merge_config({"api_key": "file-key"}, {"api_key": None})
    assert merged["api_key"] == "file-key"


def test_merge_config_explicit_cli_bool_true_applies():
    merged = merge_config({}, {"with_feedback": True})
    assert merged["with_feedback"] is True


# --- run_pipeline -----------------------------------------------------------------

def _patch_all_stages():
    return [
        patch.object(orchestrator.hygiene_module, "run_hygiene"),
        patch.object(orchestrator.crawl_module, "run_batch"),
        patch.object(orchestrator.analyze_module, "run_batch"),
        patch.object(orchestrator.contacts_module, "run_batch"),
        patch.object(orchestrator.draft_module, "run_batch"),
        patch.object(orchestrator.qa_gate_module, "run_batch"),
        patch.object(orchestrator.sender_module, "run_batch"),
        patch.object(orchestrator.sender_module, "Config"),
        patch.object(orchestrator.sender_module, "run_preflight_check"),
        patch.object(orchestrator.sender_module, "_single_mailbox_from_config"),
        patch.object(orchestrator.db_module, "get_connection"),
        patch.object(orchestrator.db_module, "init_db"),
        patch.object(orchestrator.mailboxes_module, "load_mailboxes"),
    ]


def test_run_pipeline_calls_every_stage_in_order(tmp_path):
    work_dir = tmp_path / "run1"
    paths = build_work_paths(str(work_dir))
    input_csv, offer_config = _seed_all_stage_artifacts(tmp_path, paths)

    mocks = _patch_all_stages()
    for p in mocks:
        p.start()
    try:
        orchestrator.hygiene_module.run_hygiene.return_value = {"kept": 1}
        orchestrator.crawl_module.run_batch.return_value = {"ok": 1}
        orchestrator.analyze_module.run_batch.return_value = {"ok": 1}
        orchestrator.contacts_module.run_batch.return_value = {"ok": 1}
        orchestrator.draft_module.run_batch.return_value = {"leads_drafted": 1}
        orchestrator.qa_gate_module.run_batch.return_value = {"passed": 1}
        orchestrator.sender_module.run_batch.return_value = {"sent": 1}
        orchestrator.mailboxes_module.load_mailboxes.return_value = None

        config = merge_config({
            "input_csv": input_csv, "offer_config": offer_config,
            "work_dir": str(work_dir), "api_key": "fake-key",
        }, {})
        results = run_pipeline(config)

        assert list(results.keys()) == STAGE_ORDER
        orchestrator.hygiene_module.run_hygiene.assert_called_once()
        orchestrator.crawl_module.run_batch.assert_called_once()
        orchestrator.analyze_module.run_batch.assert_called_once()
        orchestrator.contacts_module.run_batch.assert_called_once()
        orchestrator.draft_module.run_batch.assert_called_once()
        orchestrator.qa_gate_module.run_batch.assert_called_once()
        orchestrator.sender_module.run_batch.assert_called_once()
    finally:
        for p in mocks:
            p.stop()

    assert results["send"] == {"sent": 1}


def test_run_pipeline_crawl_uses_hygiene_kept_csv_as_domain_source(tmp_path):
    work_dir = tmp_path / "run1"
    paths = build_work_paths(str(work_dir))
    input_csv, offer_config = _seed_all_stage_artifacts(tmp_path, paths)

    mocks = _patch_all_stages()
    for p in mocks:
        p.start()
    try:
        orchestrator.crawl_module.run_batch.return_value = {"ok": 1}
        config = merge_config({
            "input_csv": input_csv, "offer_config": offer_config,
            "work_dir": str(work_dir), "api_key": "fake-key",
        }, {})
        run_pipeline(merge_config({**config, "input_csv": input_csv,
                                    "offer_config": offer_config}, {"only": ["crawl"]}))

        domains_arg = orchestrator.crawl_module.run_batch.call_args[0][0]
        assert domains_arg == ["acme.com"]
    finally:
        for p in mocks:
            p.stop()


def test_run_pipeline_crawl_uses_single_domain_when_given(tmp_path):
    work_dir = tmp_path / "run1"
    paths = build_work_paths(str(work_dir))
    input_csv, offer_config = _seed_all_stage_artifacts(tmp_path, paths)

    mocks = _patch_all_stages()
    for p in mocks:
        p.start()
    try:
        orchestrator.crawl_module.run_batch.return_value = {"ok": 1}
        config = merge_config({"work_dir": str(work_dir), "domain": "onlyme.com"},
                               {"only": ["crawl"]})
        run_pipeline(config)

        domains_arg = orchestrator.crawl_module.run_batch.call_args[0][0]
        assert domains_arg == ["onlyme.com"]
    finally:
        for p in mocks:
            p.stop()


def test_run_pipeline_send_loads_queue_passed_and_uses_db(tmp_path):
    work_dir = tmp_path / "run1"
    paths = build_work_paths(str(work_dir))
    Path(paths["queue_passed"]).parent.mkdir(parents=True, exist_ok=True)
    Path(paths["queue_passed"]).write_text(
        json.dumps([{"to_email": "a@b.com", "step": 1, "subject": "s", "body": "b"}]),
        encoding="utf-8")

    mocks = _patch_all_stages()
    for p in mocks:
        p.start()
    try:
        fake_conn = MagicMock()
        orchestrator.db_module.get_connection.return_value = fake_conn
        orchestrator.sender_module.run_batch.return_value = {"sent": 1}
        config = merge_config({"work_dir": str(work_dir), "sender_db": "state.db"},
                               {"only": ["send"]})
        results = run_pipeline(config)

        orchestrator.db_module.get_connection.assert_called_once_with("state.db")
        orchestrator.db_module.init_db.assert_called_once_with(fake_conn)
        queue_arg, conn_arg = orchestrator.sender_module.run_batch.call_args[0][:2]
        assert queue_arg[0]["to_email"] == "a@b.com"
        assert conn_arg is fake_conn
    finally:
        for p in mocks:
            p.stop()

    assert results["send"] == {"sent": 1}


def test_run_pipeline_creates_work_dir_even_when_first_stage_is_not_hygiene(tmp_path):
    """A real bug caught live: draft.py's real run_batch writes queue.json
    straight into work_dir with no mkdir of its own (same for qa_gate.py's
    outputs) -- fine when hygiene ran first and created the directory as a
    side effect, but a --only draft,qa_gate,send resume (analysis.json
    placed by hand or by an earlier separate run) must not crash with a
    raw FileNotFoundError just because work_dir itself was never created."""
    work_dir = tmp_path / "brand_new_run_dir"
    crawl_dir = tmp_path / "separate_crawl_dir"  # NOT nested under work_dir --
    # creating it must not accidentally also create work_dir as a side effect
    crawl_dir.mkdir(parents=True)
    assert not work_dir.exists()
    paths = build_work_paths(str(work_dir), crawl_dir_override=str(crawl_dir))
    # empty crawl_dir -- no domains, but draft.py's real run_batch still
    # unconditionally writes queue.json at the end
    offer_config = tmp_path / "offer.json"
    offer_config.write_text(json.dumps({
        "your_company_name": "x", "sender_name": "x", "what_you_sell": "x",
        "icp_description": "x", "proof_points": [{"claim": "x", "detail": "x"}], "cta_style": "x",
    }), encoding="utf-8")

    # real draft_module.run_batch -- NOT mocked -- must not crash with a
    # raw FileNotFoundError just because work_dir was never created
    config = merge_config({"work_dir": str(work_dir), "crawl_dir": str(crawl_dir),
                            "offer_config": str(offer_config)}, {"only": ["draft"]})
    run_pipeline(config)

    assert work_dir.exists()
    assert Path(paths["queue"]).exists()


def test_run_pipeline_raises_clear_error_on_missing_upstream_artifact(tmp_path):
    work_dir = tmp_path / "run1"
    config = merge_config({"work_dir": str(work_dir)}, {"only": ["send"]})
    with pytest.raises(SystemExit, match="queue_passed"):
        run_pipeline(config)


# --- run_feedback_microstage --------------------------------------------------

def test_run_feedback_microstage_classifies_then_exports(tmp_path):
    from orchestrator import run_feedback_microstage, build_work_paths as bwp

    paths = bwp(str(tmp_path / "run1"))
    Path(paths["crawl_dir"]).mkdir(parents=True)

    with patch.object(orchestrator, "db_module") as mock_db, \
         patch.object(orchestrator, "feedback_module") as mock_feedback:
        fake_conn = MagicMock()
        mock_db.get_connection.return_value = fake_conn
        mock_feedback.run_sentiment_batch.return_value = {"classified": 3}
        mock_feedback.build_few_shot_examples.return_value = [{"company_name": "Acme"}]

        result = run_feedback_microstage({"sender_db": "state.db", "api_key": "fake-key"}, paths)

        mock_db.get_connection.assert_called_once_with("state.db")
        mock_db.init_db.assert_called_once_with(fake_conn)
        mock_feedback.run_sentiment_batch.assert_called_once_with(
            fake_conn, api_key="fake-key")
        mock_feedback.build_few_shot_examples.assert_called_once_with(
            fake_conn, str(paths["crawl_dir"]), limit=5)
        fake_conn.close.assert_called_once()

    assert result == {"classified": 3, "few_shot_exported": 1}
    written = json.loads(Path(paths["few_shot"]).read_text(encoding="utf-8"))
    assert written == [{"company_name": "Acme"}]


def test_run_pipeline_with_feedback_runs_before_draft_and_feeds_few_shot_path(tmp_path):
    work_dir = tmp_path / "run1"
    paths = build_work_paths(str(work_dir))
    input_csv, offer_config = _seed_all_stage_artifacts(tmp_path, paths)

    with patch.object(orchestrator, "run_feedback_microstage") as mock_feedback_stage, \
         patch.object(orchestrator.draft_module, "run_batch") as mock_draft:
        mock_feedback_stage.return_value = {"classified": 0, "few_shot_exported": 0}
        mock_draft.return_value = {"leads_drafted": 0}

        config = merge_config({
            "offer_config": offer_config, "work_dir": str(work_dir),
            "api_key": "fake-key", "with_feedback": True,
        }, {"only": ["draft"]})
        results = run_pipeline(config)

        mock_feedback_stage.assert_called_once()
        few_shot_kwarg = mock_draft.call_args.kwargs["few_shot_path"]
        assert few_shot_kwarg == paths["few_shot"]

    assert results["feedback"] == {"classified": 0, "few_shot_exported": 0}


def test_run_pipeline_dry_run_scopes_to_send_only_not_analyze_or_draft(tmp_path):
    """dry_run is a SEND safety valve (don't actually email anyone) -- it
    must never silently make analyze/draft skip their real work too, or a
    user asking for a safe preview run would get an empty pipeline instead
    (analyze.py/draft.py's OWN --dry-run means 'log the prompt, call
    nothing', which orchestrator must not conflate with 'don't send')."""
    work_dir = tmp_path / "run1"
    paths = build_work_paths(str(work_dir))
    input_csv, offer_config = _seed_all_stage_artifacts(tmp_path, paths)

    with patch.object(orchestrator.analyze_module, "run_batch") as mock_analyze, \
         patch.object(orchestrator.draft_module, "run_batch") as mock_draft:
        mock_analyze.return_value = {"ok": 1}
        mock_draft.return_value = {"leads_drafted": 1}

        config = merge_config({
            "input_csv": input_csv, "offer_config": offer_config, "work_dir": str(work_dir),
            "api_key": "fake-key",
        }, {"only": ["analyze", "draft"], "dry_run": True})
        run_pipeline(config)

        assert mock_analyze.call_args.kwargs.get("dry_run", False) is False
        assert mock_draft.call_args.kwargs.get("dry_run", False) is False


def test_run_pipeline_threads_offer_config_into_qa_gate_for_redraft_loop(tmp_path):
    """qa_gate.py's new judge-feedback redraft loop (real feature, not a
    hypothetical) is opt-in via offer_config_path -- orchestrator already
    requires offer_config for draft, so qa_gate should get the same file
    automatically, not need a second separate config key."""
    work_dir = tmp_path / "run1"
    paths = build_work_paths(str(work_dir))
    input_csv, offer_config = _seed_all_stage_artifacts(tmp_path, paths)

    with patch.object(orchestrator.qa_gate_module, "run_batch") as mock_qa_gate:
        mock_qa_gate.return_value = {"passed": 0, "redrafted": 0}
        config = merge_config({
            "input_csv": input_csv, "offer_config": offer_config, "work_dir": str(work_dir),
            "api_key": "fake-key",
        }, {"only": ["qa_gate"]})
        run_pipeline(config)

        assert mock_qa_gate.call_args.kwargs["offer_config_path"] == offer_config


def test_run_pipeline_without_feedback_flag_skips_microstage(tmp_path):
    work_dir = tmp_path / "run1"
    paths = build_work_paths(str(work_dir))
    input_csv, offer_config = _seed_all_stage_artifacts(tmp_path, paths)

    with patch.object(orchestrator, "run_feedback_microstage") as mock_feedback_stage, \
         patch.object(orchestrator.draft_module, "run_batch") as mock_draft:
        mock_draft.return_value = {"leads_drafted": 0}

        config = merge_config({"offer_config": offer_config, "work_dir": str(work_dir),
                                "api_key": "fake-key"}, {"only": ["draft"]})
        results = run_pipeline(config)

        mock_feedback_stage.assert_not_called()

    assert "feedback" not in results


# --- load_config_file ---------------------------------------------------------

def test_load_config_file_returns_empty_dict_when_no_path_given():
    assert load_config_file(None) == {}


def test_load_config_file_reads_json_file(tmp_path):
    config_path = tmp_path / "pipeline_config.json"
    config_path.write_text(json.dumps({"work_dir": "from_config"}), encoding="utf-8")
    assert load_config_file(str(config_path)) == {"work_dir": "from_config"}


def test_load_config_file_missing_file_raises_clear_error():
    with pytest.raises(SystemExit, match="not found"):
        load_config_file("does_not_exist.json")


# --- send stage real safety checks (Config validation + SPF/DMARC preflight) ----
# sender.py's own main() never skips these before a real send; run_batch alone
# (what run_pipeline calls directly) does NOT include them -- so run_pipeline
# must replicate the exact same guard sender.py's CLI gives a standalone run.

def _seed_queue_passed(paths):
    Path(paths["queue_passed"]).parent.mkdir(parents=True, exist_ok=True)
    Path(paths["queue_passed"]).write_text("[]", encoding="utf-8")


def test_run_pipeline_send_dry_run_skips_preflight_and_config_checks(tmp_path):
    work_dir = tmp_path / "run1"
    paths = build_work_paths(str(work_dir))
    _seed_queue_passed(paths)

    mocks = _patch_all_stages()
    for p in mocks:
        p.start()
    try:
        orchestrator.sender_module.run_batch.return_value = {"sent": 0}
        config = merge_config({"work_dir": str(work_dir)}, {"only": ["send"], "dry_run": True})
        run_pipeline(config)

        orchestrator.sender_module.Config.require_send_ready.assert_not_called()
        orchestrator.sender_module.Config.require_compliance_ready.assert_not_called()
        orchestrator.sender_module.run_preflight_check.assert_not_called()
    finally:
        for p in mocks:
            p.stop()


def test_run_pipeline_send_real_run_single_mailbox_validates_and_preflights(tmp_path):
    work_dir = tmp_path / "run1"
    paths = build_work_paths(str(work_dir))
    _seed_queue_passed(paths)

    mocks = _patch_all_stages()
    for p in mocks:
        p.start()
    try:
        orchestrator.sender_module.run_batch.return_value = {"sent": 0}
        fake_single_mailbox = MagicMock(name="single_mailbox")
        orchestrator.sender_module._single_mailbox_from_config.return_value = fake_single_mailbox

        config = merge_config({"work_dir": str(work_dir), "skip_preflight": True},
                               {"only": ["send"], "dry_run": False})
        run_pipeline(config)

        orchestrator.sender_module.Config.require_send_ready.assert_called_once()
        orchestrator.sender_module.Config.require_compliance_ready.assert_not_called()
        orchestrator.sender_module.run_preflight_check.assert_called_once_with(
            [fake_single_mailbox], skip=True)
    finally:
        for p in mocks:
            p.stop()


def test_run_pipeline_send_real_run_mailbox_pool_uses_compliance_check(tmp_path):
    work_dir = tmp_path / "run1"
    paths = build_work_paths(str(work_dir))
    _seed_queue_passed(paths)

    mocks = _patch_all_stages()
    for p in mocks:
        p.start()
    try:
        orchestrator.sender_module.run_batch.return_value = {"sent": 0}
        fake_pool = [MagicMock(name="mb1"), MagicMock(name="mb2")]
        orchestrator.mailboxes_module.load_mailboxes.return_value = fake_pool

        config = merge_config({"work_dir": str(work_dir), "mailboxes": "mailboxes.json"},
                               {"only": ["send"], "dry_run": False})
        run_pipeline(config)

        orchestrator.sender_module.Config.require_compliance_ready.assert_called_once()
        orchestrator.sender_module.Config.require_send_ready.assert_not_called()
        orchestrator.sender_module.run_preflight_check.assert_called_once_with(fake_pool, skip=False)
    finally:
        for p in mocks:
            p.stop()
