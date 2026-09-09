"""
Offline unit tests -- no live network. SMTP behavior is mocked so results are
deterministic (a live mail server's catch-all/reject behavior can change run
to run, which is exactly why this shouldn't be the only test coverage).

Run with: pytest
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from contacts import (
    classify_email,
    extract_named_people,
    match_email_to_person,
    build_candidates,
    confidence_for,
    smtp_probe,
    verify_email,
    process_domain,
    apply_pattern,
    detect_pattern,
    check_catch_all,
    resolve_pattern_candidates,
    linkedin_lookup_url,
    seniority_rank,
    search_engine_fallback_people,
    opencorporates_lookup,
)


# --- classify_email ----------------------------------------------------------

def test_classify_founder_role():
    role, priority = classify_email("founder@acme.com")
    assert role == "founder_owner"
    assert priority == 1


def test_classify_sales_role():
    role, _ = classify_email("sales@acme.com")
    assert role == "sales_bd"


def test_classify_personal_pattern():
    role, _ = classify_email("jane.diaz@acme.com")
    assert role == "personal"


def test_classify_generic_contact():
    role, priority = classify_email("info@acme.com")
    assert role == "generic_contact"
    assert priority == 3


def test_classify_support_ranks_worse_than_sales():
    _, support_priority = classify_email("support@acme.com")
    _, sales_priority = classify_email("sales@acme.com")
    assert support_priority > sales_priority


def test_classify_unrecognized_falls_to_other():
    role, _ = classify_email("xyz123random@acme.com")
    assert role == "other"


# --- extract_named_people ------------------------------------------------------

def test_extract_named_people_name_then_title():
    crawl_result = {"pages": [{"page_type": "about", "text": "Jane Diaz, Founder of Acme, started the company in 2015."}]}
    people = extract_named_people(crawl_result)
    assert {"name": "Jane Diaz", "title": "Founder"} in people


def test_extract_named_people_title_then_name():
    crawl_result = {"pages": [{"page_type": "about", "text": "Our CEO: John Smith leads the team."}]}
    people = extract_named_people(crawl_result)
    names = [p["name"] for p in people]
    assert "John Smith" in names


def test_extract_named_people_ignores_irrelevant_page_types():
    crawl_result = {"pages": [{"page_type": "services", "text": "Jane Diaz, Founder of Acme."}]}
    assert extract_named_people(crawl_result) == []


def test_extract_named_people_no_match_returns_empty():
    crawl_result = {"pages": [{"page_type": "about", "text": "We build great widgets for everyone."}]}
    assert extract_named_people(crawl_result) == []


def test_extract_named_people_prefers_structural_team_members():
    # primary source: the crawler's team_members field (structural DOM extraction)
    crawl_result = {
        "team_members": [{"name": "Joel Gascoigne", "title": "CEO & Co-Founder"}],
        "pages": [],
    }
    people = extract_named_people(crawl_result)
    assert {"name": "Joel Gascoigne", "title": "CEO & Co-Founder"} in people


def test_extract_named_people_combines_structural_and_prose_sources():
    crawl_result = {
        "team_members": [{"name": "Joel Gascoigne", "title": "CEO & Co-Founder"}],
        "pages": [{"page_type": "about", "text": "Jane Diaz, Founder of Acme, started the company."}],
    }
    people = extract_named_people(crawl_result)
    names = {p["name"] for p in people}
    assert names == {"Joel Gascoigne", "Jane Diaz"}


def test_extract_named_people_dedupes_same_person_both_sources():
    crawl_result = {
        "team_members": [{"name": "Jane Diaz", "title": "Founder"}],
        "pages": [{"page_type": "about", "text": "Jane Diaz, Founder of Acme, started the company."}],
    }
    people = extract_named_people(crawl_result)
    assert len(people) == 1


def test_extract_named_people_rejects_third_party_company_mention():
    # regression: verified live on stripe.com -- a careers-page book blurb
    # reads "Aaron Levie / CEO at Box". Box's CEO must not be attributed to
    # the crawled company (stripe.com) just because the title-shape matched.
    crawl_result = {
        "domain": "stripe.com",
        "pages": [{"page_type": "careers", "text": "Aaron Levie\nCEO at Box\nis a great book about sales."}],
    }
    people = extract_named_people(crawl_result)
    names = [p["name"] for p in people]
    assert "Aaron Levie" not in names


def test_extract_named_people_keeps_match_naming_own_company():
    crawl_result = {
        "domain": "acme.com",
        "pages": [{"page_type": "about", "text": "Jane Diaz is the Founder of Acme, started in 2015."}],
    }
    people = extract_named_people(crawl_result)
    names = [p["name"] for p in people]
    assert "Jane Diaz" in names


def test_extract_named_people_keeps_match_with_no_trailing_company_at_all():
    crawl_result = {
        "domain": "acme.com",
        "pages": [{"page_type": "about", "text": "Jane Diaz, Founder, started the company in 2015."}],
    }
    people = extract_named_people(crawl_result)
    names = [p["name"] for p in people]
    assert "Jane Diaz" in names


def test_extract_named_people_first_person_self_introduction():
    # regression: verified live on basecamp.com's actual about page --
    # "Hey there, I'm Jason Fried, one of the co-founders here." is a very
    # common way small-business/solo-founder About pages are written, and
    # was invisible to both third-person patterns before this fix.
    crawl_result = {
        "domain": "basecamp.com",
        "pages": [{"page_type": "about", "text":
            "Hey there, I'm Jason Fried, one of the co-founders here. "
            "I've been running 37signals for 27 years."}],
    }
    people = extract_named_people(crawl_result)
    assert {"name": "Jason Fried", "title": "co-founder"} in people


def test_extract_named_people_self_introduction_curly_apostrophe():
    # regression: real website prose (basecamp.com's actual live text) uses
    # a typographic apostrophe (U+2019, "'"), not a straight ASCII one --
    # the first version of this fix only matched ASCII and silently missed
    # the exact real-world case it was written for.
    crawl_result = {
        "domain": "basecamp.com",
        "pages": [{"page_type": "about", "text":
            "Hey there, I’m Jason Fried, one of the co-founders here."}],
    }
    people = extract_named_people(crawl_result)
    assert {"name": "Jason Fried", "title": "co-founder"} in people


def test_extract_named_people_self_introduction_i_am_variant():
    crawl_result = {
        "domain": "acme.com",
        "pages": [{"page_type": "about", "text": "I am Jane Diaz, the Founder of Acme."}],
    }
    people = extract_named_people(crawl_result)
    names = [p["name"] for p in people]
    assert "Jane Diaz" in names


def test_extract_named_people_self_introduction_rejects_other_company():
    crawl_result = {
        "domain": "acme.com",
        "pages": [{"page_type": "about", "text": "As I'm Jane Diaz, CEO of Widgetco, I know how hard this is."}],
    }
    people = extract_named_people(crawl_result)
    names = [p["name"] for p in people]
    assert "Jane Diaz" not in names


# --- match_email_to_person -------------------------------------------------------

def test_match_email_dotted_name():
    people = [{"name": "Jane Diaz", "title": "Founder"}]
    match = match_email_to_person("jane.diaz@acme.com", people)
    assert match["name"] == "Jane Diaz"


def test_match_email_first_name_only():
    people = [{"name": "Jane Diaz", "title": "Founder"}]
    match = match_email_to_person("jane@acme.com", people)
    assert match["name"] == "Jane Diaz"


def test_match_email_no_match_for_unrelated_person():
    people = [{"name": "Jane Diaz", "title": "Founder"}]
    assert match_email_to_person("info@acme.com", people) is None


# --- build_candidates (ranking) --------------------------------------------------

def test_build_candidates_ranks_named_founder_first():
    crawl_result = {
        "domain": "acme.com",
        "emails": ["support@acme.com", "jane.diaz@acme.com", "info@acme.com"],
        "pages": [{"page_type": "about", "text": "Jane Diaz, Founder of Acme."}],
    }
    candidates, pending = build_candidates(crawl_result)
    assert candidates[0]["email"] == "jane.diaz@acme.com"
    assert candidates[0]["matched_name"] == "Jane Diaz"
    # support should rank at or near the bottom of scraped candidates
    assert candidates[-1]["email"] == "support@acme.com"
    assert pending == []  # Jane Diaz already resolved via her scraped email


def test_build_candidates_falls_back_to_guesses_when_no_emails():
    crawl_result = {"domain": "acme.com", "emails": [], "pages": []}
    candidates, pending = build_candidates(crawl_result)
    assert len(candidates) > 0
    assert all(c["source"] == "guessed" for c in candidates)
    assert all(c["email"].endswith("@acme.com") for c in candidates)
    assert pending == []  # no named people found, nothing to derive/brute-force


def test_build_candidates_scraped_beats_guessed_when_both_exist():
    # build_candidates only guesses when emails list is empty -- this documents
    # that scraped always wins by construction, not by ranking a mix.
    crawl_result = {"domain": "acme.com", "emails": ["info@acme.com"], "pages": []}
    candidates, pending = build_candidates(crawl_result)
    assert all(c["source"] == "scraped" for c in candidates)


def test_build_candidates_derives_pattern_and_applies_to_other_named_people():
    # the actual Apollo-replacement feature: one confirmed (name, email) pair
    # reveals the domain convention, which then gets applied to a second named
    # person who has no directly-published email of their own.
    crawl_result = {
        "domain": "acme.com",
        "emails": ["jane.diaz@acme.com"],
        "team_members": [
            {"name": "Jane Diaz", "title": "Founder"},
            {"name": "John Smith", "title": "Head of Sales"},
        ],
        "pages": [],
    }
    candidates, pending = build_candidates(crawl_result)
    derived = [c for c in candidates if c["source"] == "pattern_derived"]
    assert len(derived) == 1
    assert derived[0]["email"] == "john.smith@acme.com"
    assert derived[0]["matched_name"] == "John Smith"
    assert pending == []


def test_build_candidates_no_pattern_evidence_leaves_people_pending():
    crawl_result = {
        "domain": "acme.com",
        "emails": [],
        "team_members": [{"name": "John Smith", "title": "Head of Sales"}],
        "pages": [],
    }
    candidates, pending = build_candidates(crawl_result)
    assert len(pending) == 1
    assert pending[0]["name"] == "John Smith"


# --- confidence_for ------------------------------------------------------------------

def test_confidence_high_for_named_and_valid():
    c = {"matched_name": "Jane Diaz", "source": "scraped"}
    assert confidence_for(c, "valid") == "high"


def test_confidence_low_for_guessed():
    c = {"matched_name": None, "source": "guessed"}
    assert confidence_for(c, "unverified") == "low"


def test_confidence_reject_for_invalid():
    c = {"matched_name": None, "source": "scraped"}
    assert confidence_for(c, "invalid") == "reject"


def test_confidence_medium_default():
    c = {"matched_name": None, "source": "scraped"}
    assert confidence_for(c, "not_checked") == "medium"


def test_confidence_medium_for_named_but_unverified():
    # regression: verified live on basecamp.com -- jason@basecamp.com,
    # matched to real co-founder Jason Fried, got an ambiguous SMTP
    # response (450, greylisted). A real name match must not be discarded
    # down to 'low' just because SMTP verification was inconclusive --
    # SMTP unreliability is well-documented, the name match is real signal.
    c = {"matched_name": "Jason Fried", "source": "scraped"}
    assert confidence_for(c, "unverified") == "medium"


def test_confidence_low_for_unnamed_and_unverified():
    # the weakest real tier: no name backing it, and SMTP couldn't confirm
    c = {"matched_name": None, "source": "scraped"}
    assert confidence_for(c, "unverified") == "low"


def test_confidence_medium_for_named_catch_all():
    c = {"matched_name": "Jane Diaz", "source": "scraped"}
    assert confidence_for(c, "catch_all") == "medium"


def test_confidence_low_for_unnamed_catch_all():
    c = {"matched_name": None, "source": "scraped"}
    assert confidence_for(c, "catch_all") == "low"


def test_confidence_always_low_for_guessed_even_with_matched_name():
    # a blind pattern guess is a blind guess regardless of whose name it's for
    c = {"matched_name": "Jane Diaz", "source": "pattern_guessed"}
    assert confidence_for(c, "unverified") == "low"


def test_confidence_medium_for_valid_without_name_match():
    c = {"matched_name": None, "source": "scraped"}
    assert confidence_for(c, "valid") == "medium"


# --- smtp_probe / verify_email (mocked SMTP, deterministic) ------------------------

def test_smtp_probe_no_mx_returns_unverified():
    status, note = smtp_probe("a@acme.com", [])
    assert status == "unverified"
    assert "no MX" in note


@patch("contacts.smtplib.SMTP")
def test_smtp_probe_valid_on_250(mock_smtp_cls):
    instance = MagicMock()
    instance.rcpt.return_value = (250, b"OK")
    mock_smtp_cls.return_value.__enter__.return_value = instance
    status, note = smtp_probe("a@acme.com", ["mx.acme.com"])
    assert status == "valid"


@patch("contacts.smtplib.SMTP")
def test_smtp_probe_invalid_on_550(mock_smtp_cls):
    instance = MagicMock()
    instance.rcpt.return_value = (550, b"No such user")
    mock_smtp_cls.return_value.__enter__.return_value = instance
    status, note = smtp_probe("a@acme.com", ["mx.acme.com"])
    assert status == "invalid"


@patch("contacts.smtplib.SMTP")
def test_smtp_probe_connection_failure_is_unverified_not_invalid(mock_smtp_cls):
    mock_smtp_cls.return_value.__enter__.side_effect = ConnectionRefusedError("blocked")
    status, note = smtp_probe("a@acme.com", ["mx.acme.com"])
    # critical: a network-level failure (e.g. port 25 blocked) must NEVER be
    # reported as "invalid" -- that would wrongly suppress a possibly-good lead
    assert status == "unverified"


@patch("contacts.smtp_probe")
def test_verify_email_detects_catch_all(mock_probe):
    # first call (the real address) -> valid; second call (fake probe address) -> also valid
    mock_probe.side_effect = [("valid", "RCPT 250"), ("valid", "RCPT 250")]
    status, note = verify_email("real@acme.com", "acme.com", ["mx.acme.com"])
    assert status == "catch_all"


@patch("contacts.smtp_probe")
def test_verify_email_reports_valid_when_not_catch_all(mock_probe):
    mock_probe.side_effect = [("valid", "RCPT 250"), ("invalid", "RCPT 550")]
    status, note = verify_email("real@acme.com", "acme.com", ["mx.acme.com"])
    assert status == "valid"


# --- process_domain (integration, no network) --------------------------------------

def test_process_domain_no_crawl_result_file(tmp_path):
    domain_dir = tmp_path / "nosuchdata.com"
    domain_dir.mkdir()
    result = process_domain(domain_dir, verify_smtp=False)
    assert result["status"] == "no_crawl_data"


def test_process_domain_upstream_crawl_failed(tmp_path):
    domain_dir = tmp_path / "failedcrawl.com"
    domain_dir.mkdir()
    (domain_dir / "crawl_result.json").write_text(
        '{"domain": "failedcrawl.com", "status": "crawl_failed", "emails": [], "pages": []}'
    )
    result = process_domain(domain_dir, verify_smtp=False)
    assert result["status"] == "no_crawl_data"


@patch("contacts.get_mx_hosts")
def test_process_domain_verifies_against_emails_own_domain(mock_mx, tmp_path):
    # regression test: a scraped email can live on a different domain than the
    # crawled site (e.g. site is foo.so, published contact is team@makefoo.com).
    # MX/SMTP must be checked against the EMAIL's domain, not the crawled domain.
    def fake_mx(email_domain):
        return ["mx.makefoo.com"] if email_domain == "makefoo.com" else []

    mock_mx.side_effect = fake_mx
    domain_dir = tmp_path / "foo.so"
    domain_dir.mkdir()
    (domain_dir / "crawl_result.json").write_text(
        '{"domain": "foo.so", "status": "ok", '
        '"emails": ["team@makefoo.com"], "pages": []}'
    )
    result = process_domain(domain_dir, verify_smtp=False)
    assert result["status"] == "ok"
    assert result["contacts"][0]["verification"] == "not_checked"
    mock_mx.assert_called_with("makefoo.com")


@patch("contacts.get_mx_hosts")
def test_process_domain_ok_crawl_with_emails_no_verify(mock_mx, tmp_path):
    # get_mx_hosts does a real DNS lookup -- mock it so this test doesn't depend
    # on acme.com's actual live MX records (network-dependent, non-deterministic)
    mock_mx.return_value = ["mx.acme.com"]
    domain_dir = tmp_path / "acme.com"
    domain_dir.mkdir()
    (domain_dir / "crawl_result.json").write_text(
        '{"domain": "acme.com", "status": "ok", '
        '"emails": ["sales@acme.com"], "pages": []}'
    )
    result = process_domain(domain_dir, verify_smtp=False)
    assert result["status"] == "ok"
    assert result["contacts"][0]["email"] == "sales@acme.com"
    assert result["contacts"][0]["verification"] == "not_checked"


# --- apply_pattern / detect_pattern (the Apollo email-finder replacement) ----------

def test_apply_pattern_first_dot_last():
    assert apply_pattern("{f}.{l}", "Jane Diaz") == "jane.diaz"


def test_apply_pattern_first_initial_last():
    assert apply_pattern("{fi}{l}", "Jane Diaz") == "jdiaz"


def test_apply_pattern_handles_middle_name_using_first_and_last_token():
    assert apply_pattern("{f}.{l}", "Jane Q Diaz") == "jane.diaz"


def test_apply_pattern_none_for_single_word_name():
    assert apply_pattern("{f}.{l}", "Madonna") is None


def test_detect_pattern_from_one_confirmed_pair():
    pairs = [("Jane Diaz", "jane.diaz@acme.com")]
    assert detect_pattern(pairs) == "{f}.{l}"


def test_detect_pattern_first_initial_last_style():
    pairs = [("Jane Diaz", "jdiaz@acme.com")]
    assert detect_pattern(pairs) == "{fi}{l}"


def test_detect_pattern_majority_vote_across_multiple_pairs():
    pairs = [
        ("Jane Diaz", "jane.diaz@acme.com"),
        ("John Smith", "john.smith@acme.com"),
        ("Amy Lee", "amylee@acme.com"),  # one outlier shouldn't flip the result
    ]
    assert detect_pattern(pairs) == "{f}.{l}"


def test_detect_pattern_none_when_no_pairs_fit_any_template():
    pairs = [("Jane Diaz", "jd42x9@acme.com")]
    assert detect_pattern(pairs) is None


def test_detect_pattern_none_for_empty_input():
    assert detect_pattern([]) is None


# --- seniority_rank -----------------------------------------------------------------

def test_seniority_rank_founder_beats_director():
    assert seniority_rank("Founder") < seniority_rank("Director of Engineering")


def test_seniority_rank_unknown_title_ranks_last():
    assert seniority_rank("Random Title") > seniority_rank("VP of Sales")


# --- check_catch_all / resolve_pattern_candidates (mocked SMTP) --------------------

@patch("contacts.smtp_probe")
def test_check_catch_all_true_when_fake_address_accepted(mock_probe):
    mock_probe.return_value = ("valid", "RCPT 250")
    assert check_catch_all("acme.com", ["mx.acme.com"]) is True


@patch("contacts.smtp_probe")
def test_check_catch_all_false_when_fake_address_rejected(mock_probe):
    mock_probe.return_value = ("invalid", "RCPT 550")
    assert check_catch_all("acme.com", ["mx.acme.com"]) is False


def test_check_catch_all_false_with_no_mx():
    assert check_catch_all("acme.com", []) is False


@patch("contacts.smtp_probe")
def test_resolve_pattern_candidates_keeps_verified_bruteforce_match(mock_probe):
    # first template tried ({f}.{l}) fails, second ({f}{l}) succeeds
    mock_probe.side_effect = [("invalid", "550"), ("valid", "250")]
    people = [{"name": "John Smith", "title": "Head of Sales"}]
    resolved = resolve_pattern_candidates(people, "acme.com", ["mx.acme.com"],
                                           verify_smtp=True, catch_all=False)
    assert len(resolved) == 1
    assert resolved[0]["email"] == "johnsmith@acme.com"
    assert resolved[0]["verification"] == "valid"
    assert resolved[0]["source"] == "pattern_bruteforce_verified"


@patch("contacts.smtp_probe")
def test_resolve_pattern_candidates_drops_person_when_nothing_verifies(mock_probe):
    mock_probe.return_value = ("invalid", "550")
    people = [{"name": "John Smith", "title": "Head of Sales"}]
    resolved = resolve_pattern_candidates(people, "acme.com", ["mx.acme.com"],
                                           verify_smtp=True, catch_all=False)
    assert resolved == []  # never guess wrong -- say nothing instead


def test_resolve_pattern_candidates_speculative_guess_when_catch_all():
    people = [{"name": "John Smith", "title": "Head of Sales"}]
    resolved = resolve_pattern_candidates(people, "acme.com", ["mx.acme.com"],
                                           verify_smtp=True, catch_all=True)
    assert len(resolved) == 1
    assert resolved[0]["source"] == "pattern_guessed"
    assert resolved[0]["verification"] == "catch_all"


def test_resolve_pattern_candidates_ignores_non_decision_maker_titles():
    people = [{"name": "John Smith", "title": "Senior Customer Advocate"}]
    resolved = resolve_pattern_candidates(people, "acme.com", ["mx.acme.com"],
                                           verify_smtp=False, catch_all=False)
    assert resolved == []


def test_resolve_pattern_candidates_caps_at_max_bruteforce_candidates():
    people = [{"name": f"Person Number{i}", "title": "Director"} for i in range(10)]
    resolved = resolve_pattern_candidates(people, "acme.com", [], verify_smtp=False, catch_all=False)
    assert len(resolved) <= 5


# --- linkedin_lookup_url (NOT a scraper -- just a URL builder) ---------------------

def test_linkedin_lookup_url_is_a_plain_search_link_not_a_scrape():
    url = linkedin_lookup_url("Jane Diaz", "acme.com")
    assert url.startswith("https://www.google.com/search?q=")
    assert "linkedin.com" in url


# --- end-to-end: zero scraped emails, named team members only ----------------------

@patch("contacts.get_mx_hosts")
@patch("contacts.smtp_probe")
def test_process_domain_resolves_founder_via_bruteforce_when_zero_emails_scraped(mock_probe, mock_mx, tmp_path):
    # this is the buffer.com scenario verified live while building this: zero
    # emails published on-site, but a team page names the CEO -- brute force
    # should still surface a verified contact instead of falling back to a
    # generic info@ guess.
    mock_mx.return_value = ["mx.acme.com"]

    def probe_side_effect(email, mx_hosts):
        if email == "jane.diaz@acme.com":
            return "valid", "RCPT 250"
        return "invalid", "RCPT 550"

    mock_probe.side_effect = probe_side_effect
    domain_dir = tmp_path / "acme.com"
    domain_dir.mkdir()
    (domain_dir / "crawl_result.json").write_text(
        '{"domain": "acme.com", "status": "ok", "emails": [], '
        '"team_members": [{"name": "Jane Diaz", "title": "CEO & Co-Founder"}], "pages": []}'
    )
    result = process_domain(domain_dir, verify_smtp=True)
    assert result["status"] == "ok"
    emails_found = [c["email"] for c in result["contacts"]]
    assert "jane.diaz@acme.com" in emails_found
    match = next(c for c in result["contacts"] if c["email"] == "jane.diaz@acme.com")
    assert match["source"] == "pattern_bruteforce_verified"
    assert match["confidence"] == "high"


# --- search_engine_fallback_people (opt-in, lowest-trust source) -------------------
# Extends coverage beyond the crawled site's own pages -- for a domain that names
# nobody at all, this queries a public search engine's own results page (not a
# scrape of a protected platform's data) for a mention of the company's exec team.
# Deliberately opt-in and capped: gray-area ToS territory (most search engines'
# terms discourage automated querying), so this stays off by default and low-volume.
#
# Uses Bing, not DuckDuckGo -- verified live while building this: DDG's HTML
# endpoint actively CAPTCHA-walls automated requests ("Select all squares
# containing a duck"), the same category of hard anti-bot wall this project
# already refuses to bypass elsewhere. Bing's HTML results returned real
# content with no challenge under the same live test. Fixture HTML below
# mirrors Bing's real result markup (li.b_algo > .b_caption p), confirmed
# against a live response, not guessed at.

_BING_HTML_WITH_RESULT = """
<html><body><ol id="b_results">
<li class="b_algo">
  <div class="b_caption"><p>Jane Diaz, Founder of Acme Corp, spoke at the conference about scaling a small team.</p></div>
</li>
</ol></body></html>
"""

_BING_HTML_THIRD_PARTY = """
<html><body><ol id="b_results">
<li class="b_algo">
  <div class="b_caption"><p>Aaron Levie, CEO at Box, discussed enterprise software trends.</p></div>
</li>
</ol></body></html>
"""

_BING_HTML_NO_RESULTS = "<html><body><ol id=\"b_results\"></ol></body></html>"


@patch("contacts.requests.get")
def test_search_engine_fallback_extracts_name_and_title(mock_get):
    mock_get.return_value = MagicMock(status_code=200, text=_BING_HTML_WITH_RESULT)
    people = search_engine_fallback_people("Acme Corp", "acme.com")
    assert {"name": "Jane Diaz", "title": "Founder", "source": "search_fallback"} in people


@patch("contacts.requests.get")
def test_search_engine_fallback_rejects_third_party_company_mention(mock_get):
    mock_get.return_value = MagicMock(status_code=200, text=_BING_HTML_THIRD_PARTY)
    people = search_engine_fallback_people("Acme Corp", "acme.com")
    names = [p["name"] for p in people]
    assert "Aaron Levie" not in names


@patch("contacts.requests.get")
def test_search_engine_fallback_empty_on_no_results(mock_get):
    mock_get.return_value = MagicMock(status_code=200, text=_BING_HTML_NO_RESULTS)
    assert search_engine_fallback_people("Acme Corp", "acme.com") == []


@patch("contacts.requests.get")
def test_search_engine_fallback_empty_on_network_error(mock_get):
    mock_get.side_effect = Exception("network error")
    assert search_engine_fallback_people("Acme Corp", "acme.com") == []


@patch("contacts.requests.get")
def test_search_engine_fallback_caps_at_one_http_request(mock_get):
    mock_get.return_value = MagicMock(status_code=200, text=_BING_HTML_WITH_RESULT)
    search_engine_fallback_people("Acme Corp", "acme.com")
    assert mock_get.call_count == 1


# --- opencorporates_lookup (opt-in, requires the user's own free API token) -------
# Legitimate government-filed company officer/director records, aggregated by
# OpenCorporates across ~140 jurisdictions -- a real, sanctioned API relationship
# (not scraping-detection cat-and-mouse like a search engine), so this is
# structurally more durable for batch use than search_engine_fallback_people.
# Requires the caller's own free-tier token (register at
# opencorporates.com/api_accounts/new) -- this tool cannot create accounts on
# the user's behalf. Mocked tests only: no token was available this session to
# live-verify against; implemented per OpenCorporates' documented v0.4 schema.

_OC_SEARCH_RESPONSE = {
    "results": {
        "companies": [
            {"company": {"name": "ACME CORP", "company_number": "12345", "jurisdiction_code": "us_de"}}
        ]
    }
}

_OC_DETAIL_RESPONSE = {
    "results": {
        "company": {
            "name": "ACME CORP",
            "officers": [
                {"officer": {"name": "Jane Diaz", "position": "director"}},
                {"officer": {"name": "John Smith", "position": "secretary"}},
            ],
        }
    }
}


@patch("contacts.requests.get")
def test_opencorporates_lookup_extracts_officers(mock_get):
    mock_get.side_effect = [
        MagicMock(status_code=200, json=lambda: _OC_SEARCH_RESPONSE),
        MagicMock(status_code=200, json=lambda: _OC_DETAIL_RESPONSE),
    ]
    people = opencorporates_lookup("Acme Corp", api_token="fake-token")
    assert {"name": "Jane Diaz", "title": "director", "source": "opencorporates"} in people
    assert {"name": "John Smith", "title": "secretary", "source": "opencorporates"} in people


@patch("contacts.requests.get")
def test_opencorporates_lookup_empty_when_no_company_found(mock_get):
    mock_get.return_value = MagicMock(status_code=200, json=lambda: {"results": {"companies": []}})
    assert opencorporates_lookup("Nonexistent Corp", api_token="fake-token") == []


@patch("contacts.requests.get")
def test_opencorporates_lookup_empty_on_search_failure(mock_get):
    mock_get.return_value = MagicMock(status_code=401, json=lambda: {"error": "Invalid Api Token"})
    assert opencorporates_lookup("Acme Corp", api_token="bad-token") == []


@patch("contacts.requests.get")
def test_opencorporates_lookup_empty_on_network_error(mock_get):
    mock_get.side_effect = Exception("network error")
    assert opencorporates_lookup("Acme Corp", api_token="fake-token") == []


def test_opencorporates_lookup_skips_network_call_without_token():
    with patch("contacts.requests.get") as mock_get:
        result = opencorporates_lookup("Acme Corp", api_token=None)
        assert result == []
        mock_get.assert_not_called()


@patch("contacts.requests.get")
def test_opencorporates_lookup_handles_missing_officers_field(mock_get):
    mock_get.side_effect = [
        MagicMock(status_code=200, json=lambda: _OC_SEARCH_RESPONSE),
        MagicMock(status_code=200, json=lambda: {"results": {"company": {"name": "ACME CORP"}}}),
    ]
    assert opencorporates_lookup("Acme Corp", api_token="fake-token") == []


# --- extract_named_people + OpenCorporates fallback wiring -------------------------

@patch("contacts.opencorporates_lookup")
def test_extract_named_people_falls_back_to_opencorporates_when_site_names_nobody(mock_oc):
    mock_oc.return_value = [{"name": "Jane Diaz", "title": "director", "source": "opencorporates"}]
    crawl_result = {"domain": "acme.com", "team_members": [], "pages": []}
    people = extract_named_people(crawl_result, opencorporates_token="fake-token")
    assert {"name": "Jane Diaz", "title": "director", "source": "opencorporates"} in people
    mock_oc.assert_called_once()


@patch("contacts.opencorporates_lookup")
def test_extract_named_people_skips_opencorporates_when_site_already_has_people(mock_oc):
    crawl_result = {
        "domain": "acme.com",
        "team_members": [{"name": "Jane Diaz", "title": "Founder"}],
        "pages": [],
    }
    extract_named_people(crawl_result, opencorporates_token="fake-token")
    mock_oc.assert_not_called()


@patch("contacts.opencorporates_lookup")
def test_extract_named_people_skips_opencorporates_without_token(mock_oc):
    crawl_result = {"domain": "acme.com", "team_members": [], "pages": []}
    extract_named_people(crawl_result, opencorporates_token=None)
    mock_oc.assert_not_called()


@patch("contacts.opencorporates_lookup")
@patch("contacts.get_mx_hosts")
def test_process_domain_threads_opencorporates_token_through(mock_mx, mock_oc, tmp_path):
    # end-to-end: the token passed to process_domain must reach
    # opencorporates_lookup, for a domain whose site names nobody
    mock_mx.return_value = ["mx.acme.com"]
    mock_oc.return_value = [{"name": "Jane Diaz", "title": "director", "source": "opencorporates"}]
    domain_dir = tmp_path / "acme.com"
    domain_dir.mkdir()
    (domain_dir / "crawl_result.json").write_text(
        '{"domain": "acme.com", "status": "ok", "emails": [], "team_members": [], "pages": []}'
    )
    result = process_domain(domain_dir, verify_smtp=False, opencorporates_token="fake-token")
    mock_oc.assert_called_once()
    assert result["status"] == "ok"
    assert any(c["matched_name"] == "Jane Diaz" for c in result["contacts"])
