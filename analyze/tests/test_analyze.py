import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from analyze import (
    build_extraction_prompt,
    parse_extraction_response,
    normalize_quote,
    verify_quote_in_sources,
    verify_extraction_quotes,
    compute_signal_gate,
    call_gemini_extraction,
    analyze_domain,
    run_batch,
    EXTRACTION_SCHEMA_KEYS,
)


SAMPLE_CRAWL_RESULT = {
    "domain": "acme.com",
    "status": "ok",
    "pages": [
        {"url": "https://acme.com", "page_type": "home",
         "text": "Acme Corp builds project management software for construction firms. "
                  "We're hiring a Head of Sales to help us scale."},
        {"url": "https://acme.com/about", "page_type": "about",
         "text": "Founded in 2019, Acme has grown to 45 employees serving 200+ construction companies."},
    ],
}


# --- build_extraction_prompt ----------------------------------------------------

def test_build_extraction_prompt_includes_page_text_and_urls():
    prompt = build_extraction_prompt(SAMPLE_CRAWL_RESULT)
    assert "construction firms" in prompt
    assert "https://acme.com" in prompt
    assert "https://acme.com/about" in prompt


def test_build_extraction_prompt_requires_verbatim_quotes():
    prompt = build_extraction_prompt(SAMPLE_CRAWL_RESULT)
    assert "verbatim" in prompt.lower() or "exact" in prompt.lower()


def test_build_extraction_prompt_forbids_inventing_facts():
    prompt = build_extraction_prompt(SAMPLE_CRAWL_RESULT)
    assert "do not" in prompt.lower() or "never" in prompt.lower() or "only" in prompt.lower()


def test_build_extraction_prompt_truncates_to_char_budget():
    huge_result = {
        "domain": "acme.com", "status": "ok",
        "pages": [{"url": "https://acme.com", "page_type": "home", "text": "x" * 200_000}],
    }
    prompt = build_extraction_prompt(huge_result, max_chars=60_000)
    assert len(prompt) < 120_000  # bounded, not literally unlimited


# --- parse_extraction_response ------------------------------------------------

def test_parse_extraction_response_valid_json():
    raw = json.dumps({
        "company_name": "Acme", "what_they_do": "PM software", "icp_they_serve": "construction",
        "services": ["PM software"], "geo": "US", "size_estimate": "45 employees",
        "recent_events": [], "hiring_signals": ["Head of Sales"], "tech_signals": [],
        "pains": [], "hooks": [], "disqualifiers": [],
    })
    result = parse_extraction_response(raw)
    assert result["company_name"] == "Acme"
    assert result["hiring_signals"] == ["Head of Sales"]


def test_parse_extraction_response_strips_markdown_fences():
    raw = "```json\n" + json.dumps({"company_name": "Acme"}) + "\n```"
    result = parse_extraction_response(raw)
    assert result["company_name"] == "Acme"


def test_parse_extraction_response_fills_missing_keys_with_defaults():
    raw = json.dumps({"company_name": "Acme"})
    result = parse_extraction_response(raw)
    for key in EXTRACTION_SCHEMA_KEYS:
        assert key in result
    assert result["pains"] == []
    assert result["hooks"] == []


def test_parse_extraction_response_malformed_json_returns_error_sentinel():
    result = parse_extraction_response("not json at all {{{")
    assert result["_parse_error"] is True


# --- normalize_quote ---------------------------------------------------------

def test_normalize_quote_collapses_whitespace():
    assert normalize_quote("hello   world\n\n") == normalize_quote("hello world")


def test_normalize_quote_handles_curly_apostrophe():
    # real bug class hit repeatedly this session (Stage 2/4) -- a quote using
    # a typographic apostrophe must still match crawled text using the same
    assert normalize_quote("we’re hiring") == normalize_quote("we're hiring")


def test_normalize_quote_case_insensitive():
    assert normalize_quote("HELLO") == normalize_quote("hello")


# --- verify_quote_in_sources (the hallucination clamp) ----------------------

def test_verify_quote_in_sources_true_when_present():
    assert verify_quote_in_sources(
        "builds project management software", "https://acme.com", SAMPLE_CRAWL_RESULT["pages"]
    ) is True


def test_verify_quote_in_sources_false_when_fabricated():
    assert verify_quote_in_sources(
        "we are the industry leader in blockchain", "https://acme.com", SAMPLE_CRAWL_RESULT["pages"]
    ) is False


def test_verify_quote_in_sources_false_when_url_not_in_pages():
    assert verify_quote_in_sources(
        "builds project management software", "https://acme.com/nonexistent", SAMPLE_CRAWL_RESULT["pages"]
    ) is False


def test_verify_quote_in_sources_tolerates_whitespace_differences():
    assert verify_quote_in_sources(
        "builds   project management software", "https://acme.com", SAMPLE_CRAWL_RESULT["pages"]
    ) is True


# --- verify_extraction_quotes (applies the clamp across the whole extraction) -----

def test_verify_extraction_quotes_keeps_verified_hooks():
    extraction = {
        "hooks": [{"hook": "hiring for sales", "quote": "hiring a Head of Sales",
                    "source_url": "https://acme.com", "specificity": 8}],
        "pains": [], "recent_events": [],
    }
    result = verify_extraction_quotes(extraction, SAMPLE_CRAWL_RESULT["pages"])
    assert len(result["hooks"]) == 1
    assert result["quotes_dropped"] == 0


def test_verify_extraction_quotes_drops_fabricated_hooks():
    extraction = {
        "hooks": [{"hook": "fake claim", "quote": "we invented cold fusion",
                    "source_url": "https://acme.com", "specificity": 9}],
        "pains": [], "recent_events": [],
    }
    result = verify_extraction_quotes(extraction, SAMPLE_CRAWL_RESULT["pages"])
    assert len(result["hooks"]) == 0
    assert result["quotes_dropped"] == 1


def test_verify_extraction_quotes_applies_to_pains_and_recent_events_too():
    extraction = {
        "hooks": [],
        "pains": [{"pain": "fake pain", "quote": "totally fabricated quote text",
                   "source_url": "https://acme.com", "confidence": 5}],
        "recent_events": [{"event": "fake event", "quote": "another made up quote",
                            "source_url": "https://acme.com", "source_url_unused": ""}],
    }
    result = verify_extraction_quotes(extraction, SAMPLE_CRAWL_RESULT["pages"])
    assert len(result["pains"]) == 0
    assert len(result["recent_events"]) == 0
    assert result["quotes_dropped"] == 2


# --- compute_signal_gate --------------------------------------------------------

def test_signal_gate_ok_with_high_specificity_hook():
    extraction = {"hooks": [{"specificity": 8}], "disqualifiers": []}
    assert compute_signal_gate(extraction) == "ok"


def test_signal_gate_low_signal_no_qualifying_hook():
    extraction = {"hooks": [{"specificity": 3}], "disqualifiers": []}
    assert compute_signal_gate(extraction) == "low_signal"


def test_signal_gate_low_signal_no_hooks_at_all():
    extraction = {"hooks": [], "disqualifiers": []}
    assert compute_signal_gate(extraction) == "low_signal"


def test_signal_gate_low_signal_when_disqualifier_present():
    extraction = {"hooks": [{"specificity": 9}], "disqualifiers": ["competitor"]}
    assert compute_signal_gate(extraction) == "low_signal"


# --- call_gemini_extraction (mocked google.genai client) --------------------------

@patch("analyze.genai.Client")
def test_call_gemini_extraction_uses_json_mode_and_parses_json(mock_client_cls):
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.text = '{"company_name": "Acme", "what_they_do": "PM software"}'
    mock_client.models.generate_content.return_value = mock_response
    mock_client_cls.return_value = mock_client

    result = call_gemini_extraction("some prompt", api_key="fake-key", model="gemini-3.6-flash")
    assert result["company_name"] == "Acme"

    call_kwargs = mock_client.models.generate_content.call_args.kwargs
    assert call_kwargs["config"].response_mime_type == "application/json"


@patch("analyze.genai.Client")
def test_call_gemini_extraction_handles_api_error_gracefully(mock_client_cls):
    mock_client = MagicMock()
    mock_client.models.generate_content.side_effect = Exception("API error")
    mock_client_cls.return_value = mock_client

    result = call_gemini_extraction("some prompt", api_key="fake-key", model="gemini-3.6-flash")
    assert result["_api_error"] is True


def test_call_gemini_extraction_no_key_returns_error_without_calling_api():
    with patch("analyze.genai.Client") as mock_client_cls:
        result = call_gemini_extraction("prompt", api_key=None, model="gemini-3.6-flash")
        assert result["_api_error"] is True
        mock_client_cls.assert_not_called()


# --- call_gemini_extraction retry on transient errors (real bug hit live: a
# real 503 UNAVAILABLE from Google's side during this project) -----------------

@patch("analyze.time.sleep")
@patch("analyze.genai.Client")
def test_call_gemini_extraction_retries_on_503_then_succeeds(mock_client_cls, mock_sleep):
    from google.genai import errors
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.text = '{"company_name": "Acme"}'
    mock_client.models.generate_content.side_effect = [
        errors.ServerError(503, {"error": {"message": "overloaded", "status": "UNAVAILABLE"}}),
        mock_response,
    ]
    mock_client_cls.return_value = mock_client

    result = call_gemini_extraction("prompt", api_key="fake-key", model="gemini-3.6-flash")
    assert result["company_name"] == "Acme"
    assert mock_client.models.generate_content.call_count == 2
    mock_sleep.assert_called_once()


@patch("analyze.time.sleep")
@patch("analyze.genai.Client")
def test_call_gemini_extraction_gives_up_after_max_retries(mock_client_cls, mock_sleep):
    from google.genai import errors
    mock_client = MagicMock()
    mock_client.models.generate_content.side_effect = errors.ServerError(
        503, {"error": {"message": "overloaded", "status": "UNAVAILABLE"}})
    mock_client_cls.return_value = mock_client

    result = call_gemini_extraction("prompt", api_key="fake-key", model="gemini-3.6-flash")
    assert result["_api_error"] is True
    assert mock_client.models.generate_content.call_count == 3  # 1 + 2 retries


@patch("analyze.time.sleep")
@patch("analyze.genai.Client")
def test_call_gemini_extraction_does_not_retry_non_retryable_error(mock_client_cls, mock_sleep):
    from google.genai import errors
    mock_client = MagicMock()
    mock_client.models.generate_content.side_effect = errors.ClientError(
        400, {"error": {"message": "bad key", "status": "INVALID_ARGUMENT"}})
    mock_client_cls.return_value = mock_client

    result = call_gemini_extraction("prompt", api_key="fake-key", model="gemini-3.6-flash")
    assert result["_api_error"] is True
    assert mock_client.models.generate_content.call_count == 1
    mock_sleep.assert_not_called()


# --- analyze_domain (full orchestration) ------------------------------------------

@patch("analyze.call_gemini_extraction")
def test_analyze_domain_full_pipeline(mock_call):
    mock_call.return_value = {
        "company_name": "Acme", "what_they_do": "PM software for construction",
        "icp_they_serve": "construction firms", "services": ["PM software"],
        "geo": "US", "size_estimate": "45 employees",
        "recent_events": [],
        "hiring_signals": ["Head of Sales"], "tech_signals": [],
        "pains": [],
        "hooks": [{"hook": "actively hiring sales", "quote": "hiring a Head of Sales",
                    "source_url": "https://acme.com", "specificity": 8}],
        "disqualifiers": [],
    }
    result = analyze_domain(SAMPLE_CRAWL_RESULT, api_key="fake-key", model="claude-sonnet-5")
    assert result["status"] == "ok"
    assert result["signal_gate"] == "ok"
    assert result["company_name"] == "Acme"
    assert len(result["hooks"]) == 1


def test_analyze_domain_skips_non_ok_crawl_status():
    crawl_result = {"domain": "acme.com", "status": "crawl_failed", "pages": []}
    result = analyze_domain(crawl_result, api_key="fake-key", model="claude-sonnet-5")
    assert result["status"] == "no_crawl_data"


@patch("analyze.call_gemini_extraction")
def test_analyze_domain_low_signal_when_no_strong_hooks(mock_call):
    mock_call.return_value = {
        "company_name": "Acme", "what_they_do": "", "icp_they_serve": "", "services": [],
        "geo": "", "size_estimate": "", "recent_events": [], "hiring_signals": [],
        "tech_signals": [], "pains": [], "hooks": [], "disqualifiers": [],
    }
    result = analyze_domain(SAMPLE_CRAWL_RESULT, api_key="fake-key", model="claude-sonnet-5")
    assert result["signal_gate"] == "low_signal"


@patch("analyze.call_gemini_extraction")
def test_analyze_domain_propagates_api_error(mock_call):
    mock_call.return_value = {"_api_error": True, "_error_detail": "API error"}
    result = analyze_domain(SAMPLE_CRAWL_RESULT, api_key="fake-key", model="claude-sonnet-5")
    assert result["status"] == "api_error"


# --- run_batch dry-run respects crawl status (regression) -------------------------

def _write_crawl_result(crawl_dir, domain, data):
    d = crawl_dir / domain
    d.mkdir(parents=True, exist_ok=True)
    (d / "crawl_result.json").write_text(json.dumps(data), encoding="utf-8")


def test_run_batch_dry_run_skips_non_ok_crawl_status(tmp_path, caplog):
    # regression: verified live on nowsecure.nl -- dry-run built and showed a
    # prompt for a domain whose crawl_result.status was "crawl_failed",
    # misleadingly implying it would be analyzed when a real run would skip
    # it via analyze_domain's own status check. Dry-run must match reality.
    crawl_dir = tmp_path / "output"
    _write_crawl_result(crawl_dir, "failed.com", {"domain": "failed.com", "status": "crawl_failed", "pages": []})
    _write_crawl_result(crawl_dir, "ok.com", {**SAMPLE_CRAWL_RESULT, "domain": "ok.com"})

    with caplog.at_level("INFO"):
        run_batch(crawl_dir, api_key="fake-key", dry_run=True)

    messages = [r.message for r in caplog.records]
    assert any("failed.com" in m and "would skip" in m for m in messages)
    assert any("ok.com" in m and "would send" in m for m in messages)
    assert not any("failed.com" in m and "would send" in m for m in messages)
