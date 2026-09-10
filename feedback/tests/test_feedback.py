import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "sender"))

import db as db_module
import pytest
from feedback import (
    build_sentiment_prompt,
    call_gemini_sentiment,
    classify_reply_sentiment,
    run_sentiment_batch,
    variant_key,
    compute_variant_metrics,
    check_health,
    build_few_shot_examples,
    format_few_shot_block,
    DEFAULT_THRESHOLDS,
    MIN_SAMPLE_SIZE,
)


@pytest.fixture
def conn(tmp_path):
    c = db_module.get_connection(str(tmp_path / "test.db"))
    db_module.init_db(c)
    yield c
    c.close()


# --- build_sentiment_prompt / call_gemini_sentiment (mocked, honest) --------------

def test_build_sentiment_prompt_includes_reply_text():
    prompt = build_sentiment_prompt("Sounds interesting, tell me more.")
    assert "Sounds interesting, tell me more." in prompt


@patch("feedback.genai.Client")
def test_call_gemini_sentiment_uses_json_mode_and_parses_json(mock_client_cls):
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.text = json.dumps({"sentiment": "positive", "confidence": 9, "reasoning": "clear interest"})
    mock_client.models.generate_content.return_value = mock_response
    mock_client_cls.return_value = mock_client

    result = call_gemini_sentiment("prompt", api_key="fake-key", model="gemini-3.6-flash")
    assert result["sentiment"] == "positive"
    assert result["confidence"] == 9

    call_kwargs = mock_client.models.generate_content.call_args.kwargs
    assert call_kwargs["config"].response_mime_type == "application/json"


@patch("feedback.genai.Client")
def test_call_gemini_sentiment_handles_api_error(mock_client_cls):
    mock_client = MagicMock()
    mock_client.models.generate_content.side_effect = Exception("API down")
    mock_client_cls.return_value = mock_client
    result = call_gemini_sentiment("prompt", api_key="fake-key", model="gemini-3.6-flash")
    assert result["_api_error"] is True


def test_call_gemini_sentiment_no_key_skips_call():
    with patch("feedback.genai.Client") as mock_cls:
        result = call_gemini_sentiment("prompt", api_key=None, model="gemini-3.6-flash")
        assert result["_api_error"] is True
        mock_cls.assert_not_called()


# --- call_gemini_sentiment retry on transient errors ------------------------------

@patch("feedback.time.sleep")
@patch("feedback.genai.Client")
def test_call_gemini_sentiment_retries_on_503_then_succeeds(mock_client_cls, mock_sleep):
    from google.genai import errors
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.text = json.dumps({"sentiment": "positive", "confidence": 9, "reasoning": "x"})
    mock_client.models.generate_content.side_effect = [
        errors.ServerError(503, {"error": {"message": "overloaded", "status": "UNAVAILABLE"}}),
        mock_response,
    ]
    mock_client_cls.return_value = mock_client

    result = call_gemini_sentiment("prompt", api_key="fake-key", model="gemini-3.6-flash")
    assert result["sentiment"] == "positive"
    assert mock_client.models.generate_content.call_count == 2
    mock_sleep.assert_called_once()


@patch("feedback.time.sleep")
@patch("feedback.genai.Client")
def test_call_gemini_sentiment_gives_up_after_max_retries(mock_client_cls, mock_sleep):
    from google.genai import errors
    mock_client = MagicMock()
    mock_client.models.generate_content.side_effect = errors.ServerError(
        503, {"error": {"message": "overloaded", "status": "UNAVAILABLE"}})
    mock_client_cls.return_value = mock_client

    result = call_gemini_sentiment("prompt", api_key="fake-key", model="gemini-3.6-flash")
    assert result["_api_error"] is True
    assert mock_client.models.generate_content.call_count == 3


@patch("feedback.time.sleep")
@patch("feedback.genai.Client")
def test_call_gemini_sentiment_does_not_retry_non_retryable_error(mock_client_cls, mock_sleep):
    from google.genai import errors
    mock_client = MagicMock()
    mock_client.models.generate_content.side_effect = errors.ClientError(
        400, {"error": {"message": "bad key", "status": "INVALID_ARGUMENT"}})
    mock_client_cls.return_value = mock_client

    result = call_gemini_sentiment("prompt", api_key="fake-key", model="gemini-3.6-flash")
    assert result["_api_error"] is True
    assert mock_client.models.generate_content.call_count == 1
    mock_sleep.assert_not_called()


@patch("feedback.call_gemini_sentiment")
def test_classify_reply_sentiment_returns_scores(mock_call):
    mock_call.return_value = {"sentiment": "positive", "confidence": 9, "reasoning": "clear interest"}
    result = classify_reply_sentiment("Sounds great, let's talk.", api_key="fake-key", model="gemini-3.6-flash")
    assert result["sentiment"] == "positive"


# --- run_sentiment_batch (real DB, mocked Claude) ---------------------------------

@patch("feedback.classify_reply_sentiment")
def test_run_sentiment_batch_classifies_unclassified_replies(mock_classify, conn):
    mock_classify.return_value = {"sentiment": "positive", "confidence": 9, "reasoning": "interested"}
    db_module.save_reply(conn, "jane@acme.com", 1, "Re: hi", "Sounds interesting.")

    summary = run_sentiment_batch(conn, api_key="fake-key", model="gemini-3.6-flash")

    assert summary["classified"] == 1
    positive = db_module.get_replies_by_sentiment(conn, "positive")
    assert len(positive) == 1


@patch("feedback.classify_reply_sentiment")
def test_run_sentiment_batch_skips_already_classified(mock_classify, conn):
    reply_id = db_module.save_reply(conn, "jane@acme.com", 1, "Re: hi", "Sounds interesting.")
    db_module.update_reply_sentiment(conn, reply_id, "positive", confidence=9)

    run_sentiment_batch(conn, api_key="fake-key", model="gemini-3.6-flash")
    mock_classify.assert_not_called()


# --- variant_key ------------------------------------------------------------------

def test_variant_key_strips_re_prefix():
    assert variant_key("Re: quick question") == variant_key("quick question")


def test_variant_key_case_insensitive():
    assert variant_key("Quick Question") == variant_key("quick question")


def test_variant_key_strips_whitespace():
    assert variant_key("  quick question  ") == variant_key("quick question")


# --- compute_variant_metrics (real DB, real sender.db functions) -----------------

def test_compute_variant_metrics_counts_sent_and_replied(conn):
    db_module.upsert_lead(conn, "jane@acme.com")
    db_module.mark_sent(conn, "jane@acme.com", 1, "quick question", "<mid1>", "mb@x.com")
    # realistic sequence: reply_watcher.py always calls both together
    reply_id = db_module.save_reply(conn, "jane@acme.com", 1, "Re: quick question", "yes interested")
    db_module.mark_replied(conn, "jane@acme.com")
    db_module.update_reply_sentiment(conn, reply_id, "positive", confidence=9)

    metrics = compute_variant_metrics(conn)
    variant = metrics[variant_key("quick question")]
    assert variant["sent"] == 1
    assert variant["replied"] == 1
    assert variant["positive"] == 1
    assert variant["reply_rate"] == 1.0
    assert variant["positive_rate"] == 1.0


def test_compute_variant_metrics_tracks_bounce_rate(conn):
    db_module.upsert_lead(conn, "jane@acme.com")
    db_module.mark_sent(conn, "jane@acme.com", 1, "quick question", "<mid1>", "mb@x.com")
    db_module.mark_bounced(conn, "jane@acme.com")

    metrics = compute_variant_metrics(conn)
    variant = metrics[variant_key("quick question")]
    assert variant["bounced"] == 1
    assert variant["bounce_rate"] == 1.0


def test_compute_variant_metrics_flags_insufficient_sample(conn):
    db_module.upsert_lead(conn, "jane@acme.com")
    db_module.mark_sent(conn, "jane@acme.com", 1, "quick question", "<mid1>", "mb@x.com")

    metrics = compute_variant_metrics(conn)
    variant = metrics[variant_key("quick question")]
    assert variant["sufficient_sample"] is False  # only 1 sent, MIN_SAMPLE_SIZE is much higher


def test_compute_variant_metrics_separates_different_subjects(conn):
    db_module.upsert_lead(conn, "jane@acme.com")
    db_module.mark_sent(conn, "jane@acme.com", 1, "subject a", "<mid1>", "mb@x.com")
    db_module.upsert_lead(conn, "john@acme.com")
    db_module.mark_sent(conn, "john@acme.com", 1, "subject b", "<mid2>", "mb@x.com")

    metrics = compute_variant_metrics(conn)
    assert variant_key("subject a") in metrics
    assert variant_key("subject b") in metrics
    assert metrics[variant_key("subject a")]["sent"] == 1
    assert metrics[variant_key("subject b")]["sent"] == 1


# --- check_health -------------------------------------------------------------------

def test_check_health_warns_on_high_bounce_rate():
    metrics = {"bounce_rate": 0.05, "reply_rate": 0.08, "positive_rate": 0.02, "sent": 300, "sufficient_sample": True}
    warnings = check_health(metrics)
    assert any("bounce" in w.lower() for w in warnings)


def test_check_health_no_warning_within_healthy_ranges():
    metrics = {"bounce_rate": 0.01, "reply_rate": 0.08, "positive_rate": 0.02, "sent": 300, "sufficient_sample": True}
    warnings = check_health(metrics)
    assert warnings == []


def test_check_health_skips_rate_warnings_when_sample_too_small():
    # per the original design: don't draw conclusions before ~200 sends
    metrics = {"bounce_rate": 0.10, "reply_rate": 0.01, "positive_rate": 0.0, "sent": 5, "sufficient_sample": False}
    warnings = check_health(metrics)
    assert any("sample" in w.lower() for w in warnings)
    assert not any("bounce" in w.lower() for w in warnings)


def test_default_thresholds_shape():
    assert "bounce_rate_max" in DEFAULT_THRESHOLDS
    assert MIN_SAMPLE_SIZE >= 100


# --- build_few_shot_examples / format_few_shot_block (the compounding loop) -------

def test_build_few_shot_examples_joins_reply_send_log_and_analysis(conn, tmp_path):
    db_module.upsert_lead(conn, "jane@acme.com", domain="acme.com")
    db_module.mark_sent(conn, "jane@acme.com", 1, "quick question",
                         "<mid1>", "mb@x.com")
    # overwrite the send_log row's subject/body isn't tracked in send_log
    # (only subject is) -- few-shot needs the actual body too, stored via a
    # join to queue history isn't available, so this reads what IS tracked:
    # subject + the reply text + the domain's analysis.json
    reply_id = db_module.save_reply(conn, "jane@acme.com", 1, "Re: quick question", "yes, interested, let's talk")
    db_module.update_reply_sentiment(conn, reply_id, "positive", confidence=9)

    crawl_dir = tmp_path / "crawler_output"
    (crawl_dir / "acme.com").mkdir(parents=True)
    (crawl_dir / "acme.com" / "analysis.json").write_text(json.dumps({
        "domain": "acme.com", "company_name": "Acme",
        "hooks": [{"hook": "hiring for sales", "quote": "hiring a Head of Sales",
                    "source_url": "https://acme.com", "specificity": 8}],
    }), encoding="utf-8")

    examples = build_few_shot_examples(conn, str(crawl_dir), limit=5)
    assert len(examples) == 1
    assert examples[0]["subject"] == "quick question"
    assert examples[0]["company_name"] == "Acme"
    assert "hiring a Head of Sales" in examples[0]["grounding"]


def test_build_few_shot_examples_only_uses_positive_sentiment(conn, tmp_path):
    db_module.upsert_lead(conn, "jane@acme.com", domain="acme.com")
    db_module.mark_sent(conn, "jane@acme.com", 1, "quick question", "<mid1>", "mb@x.com")
    reply_id = db_module.save_reply(conn, "jane@acme.com", 1, "Re: quick question", "not interested")
    db_module.update_reply_sentiment(conn, reply_id, "negative", confidence=8)

    crawl_dir = tmp_path / "crawler_output"
    (crawl_dir / "acme.com").mkdir(parents=True)
    (crawl_dir / "acme.com" / "analysis.json").write_text(json.dumps({"domain": "acme.com"}), encoding="utf-8")

    examples = build_few_shot_examples(conn, str(crawl_dir), limit=5)
    assert examples == []


def test_build_few_shot_examples_respects_limit(conn, tmp_path):
    crawl_dir = tmp_path / "crawler_output"
    for i in range(3):
        email = f"jane{i}@acme{i}.com"
        db_module.upsert_lead(conn, email, domain=f"acme{i}.com")
        db_module.mark_sent(conn, email, 1, f"subject {i}", f"<mid{i}>", "mb@x.com")
        reply_id = db_module.save_reply(conn, email, 1, f"Re: subject {i}", "interested")
        db_module.update_reply_sentiment(conn, reply_id, "positive", confidence=9)
        (crawl_dir / f"acme{i}.com").mkdir(parents=True)
        (crawl_dir / f"acme{i}.com" / "analysis.json").write_text(
            json.dumps({"domain": f"acme{i}.com"}), encoding="utf-8")

    examples = build_few_shot_examples(conn, str(crawl_dir), limit=2)
    assert len(examples) == 2


def test_format_few_shot_block_produces_readable_text():
    examples = [{"company_name": "Acme", "grounding": "hiring a Head of Sales",
                 "subject": "quick question", "reply_text": "yes interested"}]
    block = format_few_shot_block(examples)
    assert "Acme" in block
    assert "quick question" in block
