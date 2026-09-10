import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from draft import (
    load_offer_config,
    build_draft_prompt,
    format_example_emails_block,
    parse_draft_response,
    self_check_draft,
    call_gemini_draft,
    draft_lead,
    run_batch,
    pick_best_contact,
    REQUIRED_OFFER_KEYS,
)


SAMPLE_OFFER_CONFIG = {
    "your_company_name": "Acme Consulting",
    "sender_name": "Jane Doe",
    "what_you_sell": "onboarding software for B2B SaaS companies",
    "icp_description": "B2B SaaS companies scaling their sales team",
    "proof_points": [
        {"claim": "cut onboarding time in half", "detail": "for a 40-person sales org"},
    ],
    "cta_style": "soft interest-based question, no calendar link in email 1",
}

SAMPLE_ANALYSIS = {
    "domain": "acme.com", "status": "ok", "signal_gate": "ok",
    "company_name": "Acme Corp",
    "hooks": [{"hook": "actively hiring for sales", "quote": "hiring a Head of Sales",
                "source_url": "https://acme.com", "specificity": 8}],
    "pains": [],
}

SAMPLE_CONTACT = {"email": "jane@acme.com", "matched_name": "Jane Diaz", "matched_title": "Founder"}

SAMPLE_DRAFT_SEQUENCE = [
    {"step": 1, "subject": "quick question",
     "body": "Saw you're hiring a Head of Sales -- always a sign a team's about to scale. "
             "We help companies like yours cut onboarding time in half. Worth a look?"},
    {"step": 2, "subject": "quick question",
     "body": "Bumping this in case it got buried -- still think this could help as you scale the team."},
    {"step": 3, "subject": "quick question",
     "body": "One more thought: we helped a 40-person sales org cut onboarding time in half. "
             "Happy to share how if useful."},
    {"step": 4, "subject": "quick question",
     "body": "Last note from me -- if the timing's off, no worries, and good luck with the hiring push."},
]


# --- load_offer_config -----------------------------------------------------------

def test_load_offer_config_valid_file(tmp_path):
    path = tmp_path / "offer.json"
    path.write_text(json.dumps(SAMPLE_OFFER_CONFIG), encoding="utf-8")
    config = load_offer_config(str(path))
    assert config["your_company_name"] == "Acme Consulting"


def test_load_offer_config_missing_file_raises_clear_error(tmp_path):
    with pytest.raises(SystemExit):
        load_offer_config(str(tmp_path / "does_not_exist.json"))


def test_load_offer_config_missing_required_key_raises(tmp_path):
    incomplete = {k: v for k, v in SAMPLE_OFFER_CONFIG.items() if k != "what_you_sell"}
    path = tmp_path / "offer.json"
    path.write_text(json.dumps(incomplete), encoding="utf-8")
    with pytest.raises(SystemExit):
        load_offer_config(str(path))


def test_load_offer_config_rejects_unfilled_placeholder(tmp_path):
    placeholder = {**SAMPLE_OFFER_CONFIG, "what_you_sell": "[WHAT YOU SELL]"}
    path = tmp_path / "offer.json"
    path.write_text(json.dumps(placeholder), encoding="utf-8")
    with pytest.raises(SystemExit):
        load_offer_config(str(path))


def test_required_offer_keys_nonempty():
    assert len(REQUIRED_OFFER_KEYS) > 0


def test_load_offer_config_ignores_meta_comment_field(tmp_path):
    # regression: verified live -- offer_config.example.json's own "_comment"
    # documentation field says "...replace every field..." which an earlier,
    # bare-word placeholder check incorrectly flagged as unfilled content.
    # A meta/comment key is not configured content and must not be scanned.
    with_comment = {**SAMPLE_OFFER_CONFIG, "_comment": "EXAMPLE ONLY -- replace every field with your real details"}
    path = tmp_path / "offer.json"
    path.write_text(json.dumps(with_comment), encoding="utf-8")
    config = load_offer_config(str(path))  # must NOT raise
    assert config["your_company_name"] == "Acme Consulting"


def test_load_offer_config_allows_ordinary_words_that_happen_to_match_old_markers(tmp_path):
    # a real proof point can legitimately contain a word like "replace" in
    # ordinary prose -- must not be treated as an unfilled placeholder
    real_ish = {
        **SAMPLE_OFFER_CONFIG,
        "proof_points": [{"claim": "helped them replace slow manual processes", "detail": "for a real client"}],
    }
    path = tmp_path / "offer.json"
    path.write_text(json.dumps(real_ish), encoding="utf-8")
    config = load_offer_config(str(path))  # must NOT raise
    assert "replace" in config["proof_points"][0]["claim"]


def test_load_offer_config_example_file_itself_loads_successfully():
    # the actual shipped example file must be loadable (proves the fix
    # against the real file, not just a synthetic reproduction of it)
    example_path = Path(__file__).parent.parent / "offer_config.example.json"
    config = load_offer_config(str(example_path))
    assert config["your_company_name"] == "Acme Consulting"


# --- build_draft_prompt -----------------------------------------------------------

def test_build_draft_prompt_includes_offer_and_contact_and_grounding():
    prompt = build_draft_prompt(SAMPLE_ANALYSIS, SAMPLE_CONTACT, SAMPLE_OFFER_CONFIG)
    assert "onboarding software" in prompt
    assert "Jane Diaz" in prompt
    assert "hiring a Head of Sales" in prompt
    assert "cut onboarding time in half" in prompt


def test_build_draft_prompt_requests_four_steps():
    prompt = build_draft_prompt(SAMPLE_ANALYSIS, SAMPLE_CONTACT, SAMPLE_OFFER_CONFIG)
    assert "4" in prompt or "four" in prompt.lower()


def test_build_draft_prompt_forbids_links_in_step_one():
    prompt = build_draft_prompt(SAMPLE_ANALYSIS, SAMPLE_CONTACT, SAMPLE_OFFER_CONFIG)
    assert "link" in prompt.lower()


def test_build_draft_prompt_forbids_inventing_facts():
    prompt = build_draft_prompt(SAMPLE_ANALYSIS, SAMPLE_CONTACT, SAMPLE_OFFER_CONFIG)
    assert "do not" in prompt.lower() or "only" in prompt.lower()


def test_build_draft_prompt_handles_missing_contact_name_gracefully():
    anon_contact = {"email": "info@acme.com", "matched_name": None}
    prompt = build_draft_prompt(SAMPLE_ANALYSIS, anon_contact, SAMPLE_OFFER_CONFIG)
    assert "None" not in prompt  # must not literally leak python None into the prompt


def test_build_draft_prompt_injects_few_shot_examples():
    # Stage 10's actual compounding loop: past positive-reply examples get
    # woven into the prompt so future drafts learn what worked
    few_shot = [{"company_name": "OtherCo", "grounding": "hiring a VP of Sales",
                  "subject": "worth a look", "reply_text": "yes tell me more"}]
    prompt = build_draft_prompt(SAMPLE_ANALYSIS, SAMPLE_CONTACT, SAMPLE_OFFER_CONFIG, few_shot_examples=few_shot)
    assert "OtherCo" in prompt
    assert "worth a look" in prompt


def test_build_draft_prompt_no_few_shot_section_when_none_given():
    prompt = build_draft_prompt(SAMPLE_ANALYSIS, SAMPLE_CONTACT, SAMPLE_OFFER_CONFIG)
    assert "positive reply" not in prompt.lower()


# --- bootstrap example emails (static style reference, no real replies needed) ----

def test_format_example_emails_block_produces_readable_text():
    examples = [{"subject": "quick question", "body": "Saw you're expanding into new markets. Worth a look?"}]
    block = format_example_emails_block(examples)
    assert "quick question" in block
    assert "expanding into new markets" in block


def test_build_draft_prompt_injects_bootstrap_example_emails_from_offer_config():
    config_with_examples = {
        **SAMPLE_OFFER_CONFIG,
        "example_emails": [
            {"subject": "worth a look", "body": "Noticed your team just launched a new product line. Curious how you're handling onboarding for it -- worth a quick look at what we do?"},
        ],
    }
    prompt = build_draft_prompt(SAMPLE_ANALYSIS, SAMPLE_CONTACT, config_with_examples)
    assert "launched a new product line" in prompt


def test_build_draft_prompt_no_example_emails_section_when_not_configured():
    prompt = build_draft_prompt(SAMPLE_ANALYSIS, SAMPLE_CONTACT, SAMPLE_OFFER_CONFIG)
    assert "style reference" not in prompt.lower()


def test_build_draft_prompt_example_emails_instructs_no_fact_reuse():
    config_with_examples = {
        **SAMPLE_OFFER_CONFIG,
        "example_emails": [{"subject": "hi", "body": "Some generic style example body."}],
    }
    prompt = build_draft_prompt(SAMPLE_ANALYSIS, SAMPLE_CONTACT, config_with_examples)
    assert "do not reuse" in prompt.lower() or "not real data" in prompt.lower()


def test_load_offer_config_allows_optional_example_emails_key(tmp_path):
    with_examples = {**SAMPLE_OFFER_CONFIG, "example_emails": [{"subject": "hi", "body": "example body text"}]}
    path = tmp_path / "offer.json"
    path.write_text(json.dumps(with_examples), encoding="utf-8")
    config = load_offer_config(str(path))  # must NOT raise
    assert config["example_emails"][0]["subject"] == "hi"


# --- previous_feedback: qa_gate's judge feedback looped back for a redraft --------

def test_build_draft_prompt_injects_previous_feedback():
    prompt = build_draft_prompt(SAMPLE_ANALYSIS, SAMPLE_CONTACT, SAMPLE_OFFER_CONFIG,
                                 previous_feedback="too generic, tie it to their actual product launch")
    assert "too generic, tie it to their actual product launch" in prompt
    assert "rejected" in prompt.lower()


def test_build_draft_prompt_no_previous_feedback_section_when_none_given():
    prompt = build_draft_prompt(SAMPLE_ANALYSIS, SAMPLE_CONTACT, SAMPLE_OFFER_CONFIG)
    assert "rejected" not in prompt.lower()


# --- parse_draft_response ----------------------------------------------------------

def test_parse_draft_response_valid_json_array():
    raw = json.dumps(SAMPLE_DRAFT_SEQUENCE)
    result = parse_draft_response(raw)
    assert len(result) == 4
    assert result[0]["step"] == 1


def test_parse_draft_response_strips_markdown_fences():
    raw = "```json\n" + json.dumps(SAMPLE_DRAFT_SEQUENCE) + "\n```"
    result = parse_draft_response(raw)
    assert len(result) == 4


def test_parse_draft_response_malformed_returns_empty_list():
    result = parse_draft_response("not valid json {{{")
    assert result == []


# --- self_check_draft (reuses qa_gate's real check functions) --------------------

def test_self_check_draft_passes_good_email():
    result = self_check_draft(SAMPLE_DRAFT_SEQUENCE[0])
    assert result["passed"] is True
    assert result["violations"] == []


def test_self_check_draft_catches_link_in_step_one():
    bad_email = {"step": 1, "subject": "hi", "body": "Check https://acme.com/demo out."}
    result = self_check_draft(bad_email)
    assert result["passed"] is False
    assert any("link" in v.lower() for v in result["violations"])


def test_self_check_draft_catches_banned_phrase():
    bad_email = {"step": 1, "subject": "hi", "body": "I hope this email finds you well."}
    result = self_check_draft(bad_email)
    assert result["passed"] is False


def test_self_check_draft_catches_merge_tag_leak():
    bad_email = {"step": 1, "subject": "hi {{first_name}}", "body": "some body text here"}
    result = self_check_draft(bad_email)
    assert result["passed"] is False


def test_self_check_draft_catches_over_length_body():
    bad_email = {"step": 1, "subject": "hi", "body": " ".join(["word"] * 200)}
    result = self_check_draft(bad_email)
    assert result["passed"] is False


def test_self_check_draft_step2_allows_links():
    ok_email = {"step": 2, "subject": "hi", "body": "Bumping this: https://acme.com/demo"}
    result = self_check_draft(ok_email)
    assert not any("link" in v.lower() for v in result["violations"])


# --- call_gemini_draft (mocked google.genai client) -------------------------------

@patch("draft.genai.Client")
def test_call_gemini_draft_uses_json_mode_and_parses_array(mock_client_cls):
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.text = json.dumps(SAMPLE_DRAFT_SEQUENCE)
    mock_client.models.generate_content.return_value = mock_response
    mock_client_cls.return_value = mock_client

    result = call_gemini_draft("prompt", api_key="fake-key", model="gemini-3.6-flash")
    assert len(result) == 4

    call_kwargs = mock_client.models.generate_content.call_args.kwargs
    assert call_kwargs["config"].response_mime_type == "application/json"


@patch("draft.genai.Client")
def test_call_gemini_draft_handles_api_error(mock_client_cls):
    mock_client = MagicMock()
    mock_client.models.generate_content.side_effect = Exception("API down")
    mock_client_cls.return_value = mock_client
    result = call_gemini_draft("prompt", api_key="fake-key", model="gemini-3.6-flash")
    assert result == []


def test_call_gemini_draft_no_key_returns_empty_without_calling_api():
    with patch("draft.genai.Client") as mock_cls:
        result = call_gemini_draft("prompt", api_key=None, model="gemini-3.6-flash")
        assert result == []
        mock_cls.assert_not_called()


# --- call_gemini_draft retry on transient errors ----------------------------------

@patch("draft.time.sleep")
@patch("draft.genai.Client")
def test_call_gemini_draft_retries_on_503_then_succeeds(mock_client_cls, mock_sleep):
    from google.genai import errors
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.text = json.dumps(SAMPLE_DRAFT_SEQUENCE)
    mock_client.models.generate_content.side_effect = [
        errors.ServerError(503, {"error": {"message": "overloaded", "status": "UNAVAILABLE"}}),
        mock_response,
    ]
    mock_client_cls.return_value = mock_client

    result = call_gemini_draft("prompt", api_key="fake-key", model="gemini-3.6-flash")
    assert len(result) == 4
    assert mock_client.models.generate_content.call_count == 2
    mock_sleep.assert_called_once()


@patch("draft.time.sleep")
@patch("draft.genai.Client")
def test_call_gemini_draft_gives_up_after_max_retries(mock_client_cls, mock_sleep):
    from google.genai import errors
    mock_client = MagicMock()
    mock_client.models.generate_content.side_effect = errors.ServerError(
        503, {"error": {"message": "overloaded", "status": "UNAVAILABLE"}})
    mock_client_cls.return_value = mock_client

    result = call_gemini_draft("prompt", api_key="fake-key", model="gemini-3.6-flash")
    assert result == []
    assert mock_client.models.generate_content.call_count == 3


@patch("draft.time.sleep")
@patch("draft.genai.Client")
def test_call_gemini_draft_does_not_retry_non_retryable_error(mock_client_cls, mock_sleep):
    from google.genai import errors
    mock_client = MagicMock()
    mock_client.models.generate_content.side_effect = errors.ClientError(
        400, {"error": {"message": "bad key", "status": "INVALID_ARGUMENT"}})
    mock_client_cls.return_value = mock_client

    result = call_gemini_draft("prompt", api_key="fake-key", model="gemini-3.6-flash")
    assert result == []
    assert mock_client.models.generate_content.call_count == 1
    mock_sleep.assert_not_called()


# --- draft_lead (full per-lead orchestration, with one retry on self-check fail) --

@patch("draft.call_gemini_draft")
def test_draft_lead_returns_sequence_on_success(mock_call):
    mock_call.return_value = SAMPLE_DRAFT_SEQUENCE
    result = draft_lead(SAMPLE_CONTACT, SAMPLE_ANALYSIS, SAMPLE_OFFER_CONFIG, api_key="fake-key", model="claude-opus-5")
    assert len(result) == 4
    assert result[0]["to_email"] == "jane@acme.com"
    assert result[0]["step"] == 1


@patch("draft.call_gemini_draft")
def test_draft_lead_retries_once_when_self_check_fails(mock_call):
    bad_sequence = [{"step": 1, "subject": "hi", "body": "Check https://acme.com out."}] + SAMPLE_DRAFT_SEQUENCE[1:]
    mock_call.side_effect = [bad_sequence, SAMPLE_DRAFT_SEQUENCE]
    result = draft_lead(SAMPLE_CONTACT, SAMPLE_ANALYSIS, SAMPLE_OFFER_CONFIG, api_key="fake-key", model="claude-opus-5")
    assert mock_call.call_count == 2
    assert "https://" not in result[0]["body"]


@patch("draft.call_gemini_draft")
def test_draft_lead_flags_violations_after_exhausting_retries(mock_call):
    bad_sequence = [{"step": 1, "subject": "hi", "body": "Check https://acme.com out."}] + SAMPLE_DRAFT_SEQUENCE[1:]
    mock_call.return_value = bad_sequence  # keeps failing every attempt
    result = draft_lead(SAMPLE_CONTACT, SAMPLE_ANALYSIS, SAMPLE_OFFER_CONFIG, api_key="fake-key", model="claude-opus-5")
    assert result[0]["_self_check_failed"] is True


@patch("draft.build_draft_prompt")
@patch("draft.call_gemini_draft")
def test_draft_lead_passes_few_shot_examples_to_prompt_builder(mock_call, mock_build):
    mock_build.return_value = "prompt"
    mock_call.return_value = list(SAMPLE_DRAFT_SEQUENCE)
    few_shot = [{"company_name": "OtherCo", "grounding": "hiring", "subject": "hi", "reply_text": "yes"}]

    draft_lead(SAMPLE_CONTACT, SAMPLE_ANALYSIS, SAMPLE_OFFER_CONFIG, api_key="fake-key",
               model="claude-opus-5", few_shot_examples=few_shot)

    mock_build.assert_any_call(SAMPLE_ANALYSIS, SAMPLE_CONTACT, SAMPLE_OFFER_CONFIG,
                                few_shot_examples=few_shot, previous_feedback=None)


@patch("draft.build_draft_prompt")
@patch("draft.call_gemini_draft")
def test_draft_lead_passes_previous_feedback_to_prompt_builder(mock_call, mock_build):
    # qa_gate.py's judge-feedback redraft loop: one automatic retry with the
    # judge's specific rejection reason, before an item ever reaches manual review
    mock_build.return_value = "prompt"
    mock_call.return_value = list(SAMPLE_DRAFT_SEQUENCE)

    draft_lead(SAMPLE_CONTACT, SAMPLE_ANALYSIS, SAMPLE_OFFER_CONFIG, api_key="fake-key",
               model="claude-opus-5", previous_feedback="too generic, cite the real product launch")

    mock_build.assert_any_call(SAMPLE_ANALYSIS, SAMPLE_CONTACT, SAMPLE_OFFER_CONFIG,
                                few_shot_examples=None, previous_feedback="too generic, cite the real product launch")


def test_draft_lead_empty_when_api_unavailable():
    with patch("draft.call_gemini_draft", return_value=[]):
        result = draft_lead(SAMPLE_CONTACT, SAMPLE_ANALYSIS, SAMPLE_OFFER_CONFIG, api_key=None, model="claude-opus-5")
        assert result == []


# --- pick_best_contact (selects by FINAL confidence, not list order) -------------

def test_pick_best_contact_prefers_high_over_low_regardless_of_order():
    contacts = [
        {"email": "guessed@acme.com", "confidence": "low"},
        {"email": "verified@acme.com", "confidence": "high"},
    ]
    assert pick_best_contact(contacts)["email"] == "verified@acme.com"


def test_pick_best_contact_prefers_medium_over_low():
    contacts = [{"email": "a@acme.com", "confidence": "low"}, {"email": "b@acme.com", "confidence": "medium"}]
    assert pick_best_contact(contacts)["email"] == "b@acme.com"


def test_pick_best_contact_ties_keep_first_in_list_order():
    contacts = [{"email": "first@acme.com", "confidence": "high"}, {"email": "second@acme.com", "confidence": "high"}]
    assert pick_best_contact(contacts)["email"] == "first@acme.com"


def test_pick_best_contact_missing_confidence_ranks_lowest():
    contacts = [{"email": "unknown@acme.com"}, {"email": "low@acme.com", "confidence": "low"}]
    assert pick_best_contact(contacts)["email"] == "low@acme.com"


# --- run_batch (full pipeline: contacts.json + analysis.json -> queue.json) ------

def _write_json(path, data):
    Path(path).write_text(json.dumps(data), encoding="utf-8")


@patch("draft.draft_lead")
def test_run_batch_picks_contact_by_best_final_confidence_not_list_order(mock_draft_lead, tmp_path):
    # real gap: contacts.json's own list order is scrape priority, computed
    # BEFORE the final confidence (which factors in SMTP verification) is
    # assigned -- a "low" confidence contact could still sit first
    mock_draft_lead.return_value = [{"to_email": "verified@acme.com", "subject": "s",
                                       "body": "b", "step": 1, "domain": "acme.com"}]
    crawl_dir = tmp_path / "crawler_output"
    domain_dir = crawl_dir / "acme.com"
    domain_dir.mkdir(parents=True)
    _write_json(domain_dir / "analysis.json", SAMPLE_ANALYSIS)
    _write_json(domain_dir / "contacts.json", {
        "domain": "acme.com", "status": "ok",
        "contacts": [
            {"email": "guessed@acme.com", "matched_name": None, "matched_title": None, "confidence": "low"},
            {"email": "verified@acme.com", "matched_name": "Jane Diaz", "matched_title": "Founder", "confidence": "high"},
        ],
    })
    offer_path = tmp_path / "offer.json"
    _write_json(offer_path, SAMPLE_OFFER_CONFIG)

    run_batch(str(crawl_dir), str(offer_path), str(tmp_path / "queue.json"), api_key="fake-key")

    contact_arg = mock_draft_lead.call_args.args[0]
    assert contact_arg["email"] == "verified@acme.com"


@patch("draft.call_gemini_draft")
def test_run_batch_end_to_end_produces_sender_ready_queue(mock_call, tmp_path):
    mock_call.return_value = SAMPLE_DRAFT_SEQUENCE

    crawl_dir = tmp_path / "crawler_output"
    domain_dir = crawl_dir / "acme.com"
    domain_dir.mkdir(parents=True)
    _write_json(domain_dir / "analysis.json", SAMPLE_ANALYSIS)
    _write_json(domain_dir / "contacts.json", {
        "domain": "acme.com", "status": "ok",
        "contacts": [{"email": "jane@acme.com", "matched_name": "Jane Diaz",
                       "matched_title": "Founder", "confidence": "high"}],
    })

    offer_path = tmp_path / "offer.json"
    _write_json(offer_path, SAMPLE_OFFER_CONFIG)

    queue_out = tmp_path / "queue.json"
    summary = run_batch(str(crawl_dir), str(offer_path), str(queue_out),
                         api_key="fake-key", model="claude-opus-5")

    assert summary["leads_drafted"] == 1
    queue = json.loads(queue_out.read_text(encoding="utf-8"))
    assert len(queue) == 4  # 4 sequence steps flattened
    # must be directly usable as qa_gate.py's/sender.py's --queue input
    for item in queue:
        assert set(["to_email", "subject", "body", "step", "domain"]).issubset(item.keys())
    steps = sorted(item["step"] for item in queue)
    assert steps == [1, 2, 3, 4]


@patch("draft.draft_lead")
@patch("draft.call_gemini_draft")
def test_run_batch_loads_few_shot_file_and_passes_to_draft_lead(mock_call, mock_draft_lead, tmp_path):
    mock_draft_lead.return_value = [{"to_email": "jane@acme.com", "subject": "s", "body": "b",
                                       "step": 1, "domain": "acme.com"}]

    crawl_dir = tmp_path / "crawler_output"
    domain_dir = crawl_dir / "acme.com"
    domain_dir.mkdir(parents=True)
    _write_json(domain_dir / "analysis.json", SAMPLE_ANALYSIS)
    _write_json(domain_dir / "contacts.json", {
        "domain": "acme.com", "status": "ok",
        "contacts": [{"email": "jane@acme.com", "matched_name": "Jane Diaz",
                       "matched_title": "Founder", "confidence": "high"}],
    })

    offer_path = tmp_path / "offer.json"
    _write_json(offer_path, SAMPLE_OFFER_CONFIG)

    few_shot_path = tmp_path / "few_shot.json"
    few_shot_data = [{"company_name": "OtherCo", "grounding": "hiring", "subject": "hi", "reply_text": "yes"}]
    _write_json(few_shot_path, few_shot_data)

    queue_out = tmp_path / "queue.json"
    run_batch(str(crawl_dir), str(offer_path), str(queue_out),
              api_key="fake-key", model="claude-opus-5", few_shot_path=str(few_shot_path))

    _, kwargs = mock_draft_lead.call_args
    assert kwargs["few_shot_examples"] == few_shot_data


@patch("draft.call_gemini_draft")
def test_run_batch_skips_domain_with_no_contacts(mock_call, tmp_path):
    crawl_dir = tmp_path / "crawler_output"
    domain_dir = crawl_dir / "acme.com"
    domain_dir.mkdir(parents=True)
    _write_json(domain_dir / "analysis.json", SAMPLE_ANALYSIS)
    _write_json(domain_dir / "contacts.json", {"domain": "acme.com", "status": "no_contacts_found", "contacts": []})

    offer_path = tmp_path / "offer.json"
    _write_json(offer_path, SAMPLE_OFFER_CONFIG)
    queue_out = tmp_path / "queue.json"

    summary = run_batch(str(crawl_dir), str(offer_path), str(queue_out), api_key="fake-key", model="claude-opus-5")
    assert summary["leads_drafted"] == 0
    assert summary["skipped_no_contact"] == 1
    mock_call.assert_not_called()


@patch("draft.call_gemini_draft")
def test_run_batch_skips_domain_with_no_analysis(mock_call, tmp_path):
    crawl_dir = tmp_path / "crawler_output"
    domain_dir = crawl_dir / "acme.com"
    domain_dir.mkdir(parents=True)
    _write_json(domain_dir / "contacts.json", {
        "domain": "acme.com", "status": "ok",
        "contacts": [{"email": "jane@acme.com", "matched_name": "Jane Diaz", "confidence": "high"}],
    })
    # no analysis.json written

    offer_path = tmp_path / "offer.json"
    _write_json(offer_path, SAMPLE_OFFER_CONFIG)
    queue_out = tmp_path / "queue.json"

    summary = run_batch(str(crawl_dir), str(offer_path), str(queue_out), api_key="fake-key", model="claude-opus-5")
    assert summary["skipped_no_analysis"] == 1
    mock_call.assert_not_called()
