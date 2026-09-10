import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "draft"))

import pytest
from qa_gate import (
    count_words,
    check_body_length,
    check_no_links,
    check_no_images_or_attachments,
    check_merge_tag_leaks,
    check_banned_phrases,
    count_syllables,
    estimate_reading_grade,
    check_grounding_keywords,
    run_automated_checks,
    build_judge_prompt,
    call_gemini_judge,
    judge_email,
    should_pass,
    should_sample_for_human_review,
    load_review_state,
    save_review_state,
    qa_check_item,
    run_batch,
    find_contact,
    redraft_sequence_with_feedback,
    DEFAULT_BANNED_PHRASES,
)


GOOD_EMAIL_BODY = (
    "Saw you're hiring a Head of Sales -- always a sign a team's about to scale fast. "
    "We help companies like yours cut onboarding time in half. "
    "Worth a quick look?"
)

SAMPLE_ANALYSIS = {
    "domain": "acme.com",
    "status": "ok",
    "hooks": [{"hook": "actively hiring for sales", "quote": "hiring a Head of Sales",
                "source_url": "https://acme.com", "specificity": 8}],
    "pains": [],
}


# --- count_words / check_body_length ---------------------------------------------

def test_count_words_basic():
    assert count_words("one two three") == 3


def test_check_body_length_passes_under_limit():
    passed, violations = check_body_length(GOOD_EMAIL_BODY, max_words=90)
    assert passed is True
    assert violations == []


def test_check_body_length_fails_over_limit():
    long_body = " ".join(["word"] * 200)
    passed, violations = check_body_length(long_body, max_words=90)
    assert passed is False
    assert len(violations) == 1


# --- check_no_links ---------------------------------------------------------------

def test_check_no_links_passes_clean_body():
    assert check_no_links(GOOD_EMAIL_BODY) is True


def test_check_no_links_fails_with_url():
    assert check_no_links("Check out https://acme.com/demo for more.") is False


def test_check_no_links_fails_with_bare_www():
    assert check_no_links("Visit www.acme.com today.") is False


# --- check_no_images_or_attachments -----------------------------------------------

def test_check_no_images_passes_plain_text():
    assert check_no_images_or_attachments(GOOD_EMAIL_BODY) is True


def test_check_no_images_fails_with_markdown_image():
    assert check_no_images_or_attachments("Check this out ![logo](https://acme.com/logo.png)") is False


def test_check_no_images_fails_with_html_img_tag():
    assert check_no_images_or_attachments('<img src="logo.png">') is False


# --- check_merge_tag_leaks ---------------------------------------------------------

def test_merge_tag_leaks_clean_text():
    assert check_merge_tag_leaks(GOOD_EMAIL_BODY) == []


def test_merge_tag_leaks_detects_double_curly():
    result = check_merge_tag_leaks("Hi {{first_name}}, saw your site.")
    assert len(result) > 0


def test_merge_tag_leaks_detects_double_bracket():
    result = check_merge_tag_leaks("Hi [[name]], saw your site.")
    assert len(result) > 0


def test_merge_tag_leaks_detects_ai_disclosure_phrases():
    result = check_merge_tag_leaks("As an AI, I don't have access to real-time data.")
    assert len(result) > 0


def test_merge_tag_leaks_detects_literal_undefined():
    result = check_merge_tag_leaks("Hi undefined, saw your site.")
    assert len(result) > 0


# --- check_banned_phrases -----------------------------------------------------------

def test_banned_phrases_clean_text():
    assert check_banned_phrases(GOOD_EMAIL_BODY) == []


def test_banned_phrases_detects_default_list_hit():
    result = check_banned_phrases("I hope this email finds you well.")
    assert len(result) > 0


def test_banned_phrases_case_insensitive():
    result = check_banned_phrases("Just following up on my last email.")
    assert any("following up" in r.lower() for r in result)


def test_banned_phrases_custom_list():
    result = check_banned_phrases("This is revolutionary technology.", banned_list=["revolutionary"])
    assert len(result) == 1


def test_default_banned_phrases_nonempty():
    assert len(DEFAULT_BANNED_PHRASES) > 0


# --- reading grade -------------------------------------------------------------------

def test_count_syllables_simple_word():
    assert count_syllables("cat") == 1


def test_count_syllables_multi_syllable_word():
    assert count_syllables("beautiful") >= 3


def test_estimate_reading_grade_simple_sentence_low_grade():
    grade = estimate_reading_grade("The cat sat on the mat. It was a nice day.")
    assert grade < 6


def test_estimate_reading_grade_complex_sentence_higher_grade():
    simple_grade = estimate_reading_grade("The cat sat on the mat.")
    complex_grade = estimate_reading_grade(
        "The multifaceted implementation of organizational infrastructure "
        "necessitates comprehensive stakeholder deliberation."
    )
    assert complex_grade > simple_grade


def test_estimate_reading_grade_empty_text_returns_zero():
    assert estimate_reading_grade("") == 0


# --- check_grounding_keywords (cheap automated pre-check) -------------------------

def test_grounding_keywords_true_when_body_references_verified_hook():
    body = "Saw you're hiring a Head of Sales -- exciting time to scale."
    assert check_grounding_keywords(body, SAMPLE_ANALYSIS) is True


def test_grounding_keywords_false_when_body_is_generic():
    body = "We help companies grow revenue with our amazing platform."
    assert check_grounding_keywords(body, SAMPLE_ANALYSIS) is False


def test_grounding_keywords_false_when_no_analysis_available():
    assert check_grounding_keywords(GOOD_EMAIL_BODY, None) is False


def test_grounding_keywords_false_when_analysis_has_no_hooks():
    assert check_grounding_keywords(GOOD_EMAIL_BODY, {"hooks": [], "pains": []}) is False


# --- run_automated_checks (combines everything) -----------------------------------

def test_run_automated_checks_passes_good_email():
    item = {"subject": "quick question", "body": GOOD_EMAIL_BODY, "step": 1}
    result = run_automated_checks(item, SAMPLE_ANALYSIS)
    assert result["passed"] is True
    assert result["violations"] == []


def test_run_automated_checks_fails_on_multiple_violations():
    item = {"subject": "hi {{first_name}}", "body": "I hope this email finds you well. " * 30, "step": 1}
    result = run_automated_checks(item, SAMPLE_ANALYSIS)
    assert result["passed"] is False
    assert len(result["violations"]) > 1


def test_run_automated_checks_step1_rejects_links():
    item = {"subject": "hi", "body": "Check https://acme.com/demo out.", "step": 1}
    result = run_automated_checks(item, SAMPLE_ANALYSIS)
    assert result["passed"] is False
    assert any("link" in v.lower() for v in result["violations"])


def test_run_automated_checks_step2_allows_links():
    # only email 1 is required link-free per the original design
    item = {"subject": "hi", "body": "Bumping this: https://acme.com/demo", "step": 2}
    result = run_automated_checks(item, SAMPLE_ANALYSIS)
    assert not any("link" in v.lower() for v in result["violations"])


def test_run_automated_checks_grounding_only_required_on_step_1():
    # regression: verified live via real Draft -> QA gate integration test --
    # a real 4-step sequence's step-2/3/4 emails (follow-up, proof-point,
    # breakup -- per the original sequence design, none of which necessarily
    # re-reference the SAME verified hook step 1 used) were all incorrectly
    # failing here. Only the cold-open (step 1) needs source-grounding
    # discipline; a breakup email genuinely won't cite a specific fact.
    generic_followup = {"subject": "hi", "body": "Bumping this in case it got buried.", "step": 2}
    result = run_automated_checks(generic_followup, SAMPLE_ANALYSIS)
    assert not any("grounded" in v.lower() or "verified detail" in v.lower() for v in result["violations"])


def test_run_automated_checks_grounding_still_required_on_step_1():
    # the fix must not simply disable grounding everywhere -- step 1 still needs it
    generic_intro = {"subject": "hi", "body": "We help companies grow revenue with our platform.", "step": 1}
    result = run_automated_checks(generic_intro, SAMPLE_ANALYSIS)
    assert any("verified detail" in v.lower() for v in result["violations"])


# --- LLM judge (mocked Claude, honest -- no live key available) -------------------

def test_build_judge_prompt_includes_email_and_grounding_source():
    prompt = build_judge_prompt({"subject": "hi", "body": GOOD_EMAIL_BODY}, SAMPLE_ANALYSIS)
    assert GOOD_EMAIL_BODY in prompt
    assert "hiring a Head of Sales" in prompt


@patch("qa_gate.genai.Client")
def test_call_gemini_judge_uses_json_mode_and_parses_json(mock_client_cls):
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.text = json.dumps({
        "grounding": 9, "specificity": 8, "peer_tone": 7, "would_reply": 7,
        "overall": 8, "feedback": "solid",
    })
    mock_client.models.generate_content.return_value = mock_response
    mock_client_cls.return_value = mock_client

    result = call_gemini_judge("some prompt", api_key="fake-key", model="gemini-3.6-flash")
    assert result["grounding"] == 9
    assert result["overall"] == 8

    call_kwargs = mock_client.models.generate_content.call_args.kwargs
    assert call_kwargs["config"].response_mime_type == "application/json"


@patch("qa_gate.genai.Client")
def test_call_gemini_judge_handles_api_error(mock_client_cls):
    mock_client = MagicMock()
    mock_client.models.generate_content.side_effect = Exception("API down")
    mock_client_cls.return_value = mock_client
    result = call_gemini_judge("prompt", api_key="fake-key", model="gemini-3.6-flash")
    assert result["_api_error"] is True


def test_call_gemini_judge_no_key_skips_api_call():
    with patch("qa_gate.genai.Client") as mock_cls:
        result = call_gemini_judge("prompt", api_key=None, model="gemini-3.6-flash")
        assert result["_api_error"] is True
        mock_cls.assert_not_called()


# --- call_gemini_judge retry on transient errors ----------------------------------

@patch("qa_gate.time.sleep")
@patch("qa_gate.genai.Client")
def test_call_gemini_judge_retries_on_503_then_succeeds(mock_client_cls, mock_sleep):
    from google.genai import errors
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.text = json.dumps({"grounding": 9, "overall": 8})
    mock_client.models.generate_content.side_effect = [
        errors.ServerError(503, {"error": {"message": "overloaded", "status": "UNAVAILABLE"}}),
        mock_response,
    ]
    mock_client_cls.return_value = mock_client

    result = call_gemini_judge("prompt", api_key="fake-key", model="gemini-3.6-flash")
    assert result["overall"] == 8
    assert mock_client.models.generate_content.call_count == 2
    mock_sleep.assert_called_once()


@patch("qa_gate.time.sleep")
@patch("qa_gate.genai.Client")
def test_call_gemini_judge_gives_up_after_max_retries(mock_client_cls, mock_sleep):
    from google.genai import errors
    mock_client = MagicMock()
    mock_client.models.generate_content.side_effect = errors.ServerError(
        503, {"error": {"message": "overloaded", "status": "UNAVAILABLE"}})
    mock_client_cls.return_value = mock_client

    result = call_gemini_judge("prompt", api_key="fake-key", model="gemini-3.6-flash")
    assert result["_api_error"] is True
    assert mock_client.models.generate_content.call_count == 3


@patch("qa_gate.time.sleep")
@patch("qa_gate.genai.Client")
def test_call_gemini_judge_does_not_retry_non_retryable_error(mock_client_cls, mock_sleep):
    from google.genai import errors
    mock_client = MagicMock()
    mock_client.models.generate_content.side_effect = errors.ClientError(
        400, {"error": {"message": "bad key", "status": "INVALID_ARGUMENT"}})
    mock_client_cls.return_value = mock_client

    result = call_gemini_judge("prompt", api_key="fake-key", model="gemini-3.6-flash")
    assert result["_api_error"] is True
    assert mock_client.models.generate_content.call_count == 1
    mock_sleep.assert_not_called()


# --- should_pass ---------------------------------------------------------------------

def test_should_pass_true_when_both_thresholds_met():
    assert should_pass({"grounding": 8, "overall": 7}) is True


def test_should_pass_false_when_grounding_below_threshold():
    assert should_pass({"grounding": 7, "overall": 9}) is False


def test_should_pass_false_when_overall_below_threshold():
    assert should_pass({"grounding": 10, "overall": 6}) is False


def test_should_pass_false_on_api_error():
    assert should_pass({"_api_error": True}) is False


# --- human review sampling ("first 50, then 10% + borderline") -------------------

def test_should_sample_first_50_always_true():
    for i in range(50):
        assert should_sample_for_human_review(i, {"overall": 10, "grounding": 10}) is True


def test_should_sample_after_50_borderline_score_always_true():
    assert should_sample_for_human_review(100, {"overall": 7, "grounding": 8}) is True


def test_should_sample_after_50_strong_score_not_always_true():
    # can't assert False deterministically (10% random sampling) -- assert
    # it's not ALWAYS true across many strong-scoring calls past the first 50
    results = [should_sample_for_human_review(1000 + i, {"overall": 10, "grounding": 10}) for i in range(200)]
    assert not all(results)
    assert any(results)  # but some should still get sampled (~10%)


# --- review state persistence (stateful across batch runs) -----------------------

def test_load_review_state_zero_when_no_file(tmp_path):
    assert load_review_state(str(tmp_path / "state.json")) == 0


def test_save_and_load_review_state_roundtrip(tmp_path):
    path = str(tmp_path / "state.json")
    save_review_state(path, 42)
    assert load_review_state(path) == 42


# --- qa_check_item (full per-item orchestration) ----------------------------------

@patch("qa_gate.call_gemini_judge")
def test_qa_check_item_passes_good_email(mock_judge):
    mock_judge.return_value = {"grounding": 9, "specificity": 8, "peer_tone": 8,
                                "would_reply": 8, "overall": 8, "feedback": "good"}
    item = {"to_email": "lead@acme.com", "subject": "quick question", "body": GOOD_EMAIL_BODY, "step": 1}
    result = qa_check_item(item, SAMPLE_ANALYSIS, api_key="fake-key", model="claude-sonnet-5",
                            reviewed_so_far=100)
    assert result["verdict"] == "pass"


def test_qa_check_item_fails_fast_on_automated_violation_no_api_call():
    item = {"to_email": "lead@acme.com", "subject": "hi {{name}}", "body": "bad", "step": 1}
    with patch("qa_gate.call_gemini_judge") as mock_judge:
        result = qa_check_item(item, SAMPLE_ANALYSIS, api_key="fake-key", model="claude-sonnet-5",
                                reviewed_so_far=100)
        assert result["verdict"] == "fail_automated"
        mock_judge.assert_not_called()  # don't waste an API call on an obviously broken email


@patch("qa_gate.call_gemini_judge")
def test_qa_check_item_needs_review_within_first_50(mock_judge):
    mock_judge.return_value = {"grounding": 9, "overall": 9}
    item = {"to_email": "lead@acme.com", "subject": "hi", "body": GOOD_EMAIL_BODY, "step": 1}
    result = qa_check_item(item, SAMPLE_ANALYSIS, api_key="fake-key", model="claude-sonnet-5",
                            reviewed_so_far=5)
    assert result["needs_human_review"] is True


# --- run_batch (full pipeline, real file I/O) -------------------------------------

def _write_json(path, data):
    Path(path).write_text(json.dumps(data), encoding="utf-8")


@patch("qa_gate.call_gemini_judge")
def test_run_batch_end_to_end_writes_passed_and_review_files(mock_judge, tmp_path):
    mock_judge.return_value = {"grounding": 9, "specificity": 8, "peer_tone": 8,
                                "would_reply": 8, "overall": 9, "feedback": "great"}

    queue_path = tmp_path / "queue.json"
    _write_json(queue_path, [
        {"to_email": "jane@acme.com", "subject": "quick question", "body": GOOD_EMAIL_BODY, "step": 1},
    ])

    crawl_dir = tmp_path / "crawler_output"
    (crawl_dir / "acme.com").mkdir(parents=True)
    _write_json(crawl_dir / "acme.com" / "analysis.json", SAMPLE_ANALYSIS)

    passed_out = tmp_path / "queue_passed.json"
    review_out = tmp_path / "manual_review.csv"
    state_path = tmp_path / "state.json"

    # simulate 100 already-reviewed so this run isn't forced into the first-50 review bucket
    save_review_state(str(state_path), 100)

    summary = run_batch(str(queue_path), str(crawl_dir), str(passed_out), str(review_out),
                         api_key="fake-key", model="claude-sonnet-5", state_path=str(state_path))

    assert summary["passed"] == 1
    passed_items = json.loads(passed_out.read_text(encoding="utf-8"))
    assert passed_items[0]["to_email"] == "jane@acme.com"
    # output must be directly usable as sender.py's --queue input -- same keys, nothing extra required
    assert set(["to_email", "subject", "body", "step"]).issubset(passed_items[0].keys())


@patch("qa_gate.call_gemini_judge")
def test_run_batch_routes_failures_to_review_csv(mock_judge, tmp_path):
    queue_path = tmp_path / "queue.json"
    _write_json(queue_path, [
        {"to_email": "bad@acme.com", "subject": "hi {{name}}", "body": "broken", "step": 1},
    ])
    crawl_dir = tmp_path / "crawler_output"
    crawl_dir.mkdir()
    passed_out = tmp_path / "queue_passed.json"
    review_out = tmp_path / "manual_review.csv"

    summary = run_batch(str(queue_path), str(crawl_dir), str(passed_out), str(review_out),
                         api_key="fake-key", model="claude-sonnet-5", state_path=str(tmp_path / "state.json"))

    assert summary["failed_automated"] == 1
    assert review_out.exists()
    mock_judge.assert_not_called()


# --- find_contact -------------------------------------------------------------------

def test_find_contact_returns_matching_contact_by_email(tmp_path):
    crawl_dir = tmp_path / "crawler_output"
    (crawl_dir / "acme.com").mkdir(parents=True)
    _write_json(crawl_dir / "acme.com" / "contacts.json", {
        "domain": "acme.com", "status": "ok",
        "contacts": [{"email": "jane@acme.com", "matched_name": "Jane Diaz", "matched_title": "Founder"}],
    })
    contact = find_contact(crawl_dir, "acme.com", "JANE@acme.com")  # case-insensitive
    assert contact["matched_name"] == "Jane Diaz"


def test_find_contact_falls_back_to_minimal_dict_when_not_found(tmp_path):
    crawl_dir = tmp_path / "crawler_output"
    (crawl_dir / "acme.com").mkdir(parents=True)
    _write_json(crawl_dir / "acme.com" / "contacts.json", {
        "domain": "acme.com", "status": "ok",
        "contacts": [{"email": "someone-else@acme.com", "matched_name": "Someone Else"}],
    })
    contact = find_contact(crawl_dir, "acme.com", "jane@acme.com")
    assert contact["email"] == "jane@acme.com"
    assert contact["matched_name"] is None


def test_find_contact_falls_back_when_contacts_file_missing(tmp_path):
    crawl_dir = tmp_path / "crawler_output"
    (crawl_dir / "acme.com").mkdir(parents=True)
    contact = find_contact(crawl_dir, "acme.com", "jane@acme.com")
    assert contact == {"email": "jane@acme.com", "matched_name": None, "matched_title": None}


# --- redraft_sequence_with_feedback (real cross-module reuse of draft.py) ---------

SAMPLE_OFFER_CONFIG = {
    "your_company_name": "Acme Consulting", "sender_name": "Jane Doe",
    "what_you_sell": "onboarding software", "icp_description": "B2B SaaS",
    "proof_points": [{"claim": "cut onboarding time in half", "detail": "for a real client"}],
    "cta_style": "soft question",
}


@patch("draft.draft_lead")
def test_redraft_sequence_with_feedback_calls_draft_lead_with_feedback(mock_draft_lead, tmp_path):
    crawl_dir = tmp_path / "crawler_output"
    (crawl_dir / "acme.com").mkdir(parents=True)
    _write_json(crawl_dir / "acme.com" / "contacts.json", {
        "domain": "acme.com", "status": "ok",
        "contacts": [{"email": "jane@acme.com", "matched_name": "Jane Diaz", "matched_title": "Founder"}],
    })
    mock_draft_lead.return_value = [{"to_email": "jane@acme.com", "subject": "redrafted",
                                       "body": "new body", "step": 1, "domain": "acme.com"}]

    result = redraft_sequence_with_feedback(
        "acme.com", "jane@acme.com", SAMPLE_ANALYSIS, SAMPLE_OFFER_CONFIG,
        "too generic, cite the real hook", api_key="fake-key", model="gemini-3.6-flash",
        crawl_dir=crawl_dir)

    assert result[0]["subject"] == "redrafted"
    call_kwargs = mock_draft_lead.call_args.kwargs
    assert call_kwargs["previous_feedback"] == "too generic, cite the real hook"
    contact_arg = mock_draft_lead.call_args.args[0]
    assert contact_arg["matched_name"] == "Jane Diaz"


# --- run_batch: judge-feedback redraft loop (opt-in via offer_config_path) --------

def test_run_batch_redrafts_step1_judge_failure_when_offer_config_given(tmp_path):
    queue_path = tmp_path / "queue.json"
    _write_json(queue_path, [
        {"to_email": "jane@acme.com", "subject": "original subject",
         "body": GOOD_EMAIL_BODY, "step": 1, "domain": "acme.com"},
    ])
    crawl_dir = tmp_path / "crawler_output"
    (crawl_dir / "acme.com").mkdir(parents=True)
    _write_json(crawl_dir / "acme.com" / "analysis.json", SAMPLE_ANALYSIS)
    _write_json(crawl_dir / "acme.com" / "contacts.json", {
        "domain": "acme.com", "status": "ok",
        "contacts": [{"email": "jane@acme.com", "matched_name": "Jane Diaz", "matched_title": "Founder"}],
    })
    offer_config_path = tmp_path / "offer.json"
    _write_json(offer_config_path, SAMPLE_OFFER_CONFIG)

    redrafted_items = [{"to_email": "jane@acme.com", "subject": "redrafted subject",
                          "body": GOOD_EMAIL_BODY, "step": 1, "domain": "acme.com"}]

    def judge_side_effect(item, analysis, api_key, model):
        if item["subject"] == "original subject":
            return {"grounding": 3, "overall": 4, "feedback": "too generic"}
        return {"grounding": 9, "overall": 9, "feedback": "great"}

    with patch("qa_gate.judge_email", side_effect=judge_side_effect), \
         patch("qa_gate.redraft_sequence_with_feedback", return_value=redrafted_items) as mock_redraft:

        summary = run_batch(str(queue_path), str(crawl_dir), str(tmp_path / "queue_passed.json"),
                             str(tmp_path / "manual_review.csv"), api_key="fake-key",
                             state_path=str(tmp_path / "state.json"), offer_config_path=str(offer_config_path))

    mock_redraft.assert_called_once()
    assert mock_redraft.call_args.kwargs["feedback"] == "too generic"
    assert summary["redrafted"] == 1
    assert summary["passed"] == 1
    passed = json.loads((tmp_path / "queue_passed.json").read_text(encoding="utf-8"))
    assert passed[0]["subject"] == "redrafted subject"


def test_run_batch_does_not_redraft_when_offer_config_not_given(tmp_path):
    queue_path = tmp_path / "queue.json"
    _write_json(queue_path, [
        {"to_email": "jane@acme.com", "subject": "s", "body": GOOD_EMAIL_BODY, "step": 1, "domain": "acme.com"},
    ])
    crawl_dir = tmp_path / "crawler_output"
    (crawl_dir / "acme.com").mkdir(parents=True)
    _write_json(crawl_dir / "acme.com" / "analysis.json", SAMPLE_ANALYSIS)

    with patch("qa_gate.judge_email", return_value={"grounding": 2, "overall": 2, "feedback": "bad"}), \
         patch("qa_gate.redraft_sequence_with_feedback") as mock_redraft:
        summary = run_batch(str(queue_path), str(crawl_dir), str(tmp_path / "queue_passed.json"),
                             str(tmp_path / "manual_review.csv"), api_key="fake-key",
                             state_path=str(tmp_path / "state.json"))

    mock_redraft.assert_not_called()
    assert summary.get("redrafted", 0) == 0
    assert summary["failed_judge"] == 1


def test_run_batch_does_not_redraft_on_automated_failure(tmp_path):
    queue_path = tmp_path / "queue.json"
    _write_json(queue_path, [
        {"to_email": "jane@acme.com", "subject": "hi {{name}}", "body": "broken",
         "step": 1, "domain": "acme.com"},
    ])
    crawl_dir = tmp_path / "crawler_output"
    (crawl_dir / "acme.com").mkdir(parents=True)
    _write_json(crawl_dir / "acme.com" / "analysis.json", SAMPLE_ANALYSIS)
    offer_config_path = tmp_path / "offer.json"
    _write_json(offer_config_path, SAMPLE_OFFER_CONFIG)

    with patch("qa_gate.redraft_sequence_with_feedback") as mock_redraft:
        summary = run_batch(str(queue_path), str(crawl_dir), str(tmp_path / "queue_passed.json"),
                             str(tmp_path / "manual_review.csv"), api_key="fake-key",
                             state_path=str(tmp_path / "state.json"), offer_config_path=str(offer_config_path))

    mock_redraft.assert_not_called()
    assert summary["failed_automated"] == 1


def test_run_batch_keeps_original_when_redraft_returns_nothing(tmp_path):
    queue_path = tmp_path / "queue.json"
    _write_json(queue_path, [
        {"to_email": "jane@acme.com", "subject": "original subject",
         "body": GOOD_EMAIL_BODY, "step": 1, "domain": "acme.com"},
    ])
    crawl_dir = tmp_path / "crawler_output"
    (crawl_dir / "acme.com").mkdir(parents=True)
    _write_json(crawl_dir / "acme.com" / "analysis.json", SAMPLE_ANALYSIS)
    offer_config_path = tmp_path / "offer.json"
    _write_json(offer_config_path, SAMPLE_OFFER_CONFIG)

    with patch("qa_gate.judge_email", return_value={"grounding": 2, "overall": 2, "feedback": "too generic"}), \
         patch("qa_gate.redraft_sequence_with_feedback", return_value=[]) as mock_redraft:
        summary = run_batch(str(queue_path), str(crawl_dir), str(tmp_path / "queue_passed.json"),
                             str(tmp_path / "manual_review.csv"), api_key="fake-key",
                             state_path=str(tmp_path / "state.json"), offer_config_path=str(offer_config_path))

    mock_redraft.assert_called_once()
    assert summary["failed_judge"] == 1
    passed = json.loads((tmp_path / "queue_passed.json").read_text(encoding="utf-8"))
    assert passed == []
