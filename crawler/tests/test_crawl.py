"""
Offline unit tests -- no network, no browser. Run with: pytest

These pin down the logic that's easy to silently break while tweaking the
crawler (email filtering, domain normalization, block detection, cache
freshness) so a future change gets caught before it burns 850 live domains.
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from unittest.mock import MagicMock, patch

import pytest

from crawl import (
    normalize_domain,
    discover_links,
    extract_emails,
    looks_blocked,
    is_cache_fresh,
    merge_discovery,
    classify_error,
    extract_team_members_from_html,
    extract_jsonld_people,
    discover_via_sitemap,
    extract_clean_text,
    extract_pdf_text,
    is_pdf_url,
    fetch_pdf_bytes,
    build_context_kwargs,
    capture_screenshot,
)


# --- normalize_domain -------------------------------------------------------

def test_normalize_domain_strips_scheme_and_path():
    assert normalize_domain("https://www.Example.com/foo?x=1") == "example.com"


def test_normalize_domain_bare_domain():
    assert normalize_domain("example.com") == "example.com"


def test_normalize_domain_subdomain_collapses_to_registrable_root():
    assert normalize_domain("blog.example.co.uk") == "example.co.uk"


def test_normalize_domain_empty_input():
    assert normalize_domain("") == ""


def test_normalize_domain_garbage_input():
    assert normalize_domain("not a url at all!!") == ""


# --- extract_emails ----------------------------------------------------------

def test_extract_emails_finds_mailto():
    html = '<a href="mailto:sales@acme.com">Email us</a>'
    assert extract_emails(html) == ["sales@acme.com"]


def test_extract_emails_drops_placeholder_domains():
    html = """
    <a href="mailto:jane@acme.com">Jane</a>
    <p>e.g. john.doe@example.com or test@yourcompany.com</p>
    """
    result = extract_emails(html, site_domain="acme.com")
    assert result == ["jane@acme.com"]


def test_extract_emails_drops_image_filenames_matched_by_regex():
    html = '<p>logo@2x.png is not an email</p>'
    assert extract_emails(html) == []


def test_extract_emails_sorts_site_domain_first():
    html = """
    <a href="mailto:info@thirdpartywidget.io">widget</a>
    <a href="mailto:sales@acme.com">sales</a>
    """
    result = extract_emails(html, site_domain="acme.com")
    assert result[0] == "sales@acme.com"
    assert "info@thirdpartywidget.io" in result


# --- discover_links ------------------------------------------------------------

def test_discover_links_finds_about_and_contact():
    html = """
    <nav>
      <a href="/about-us">About Us</a>
      <a href="/contact">Contact</a>
      <a href="/random-page">Random</a>
    </nav>
    """
    found = discover_links("https://acme.com", html)
    assert found["about"] == ["https://acme.com/about-us"]
    assert found["contact"] == ["https://acme.com/contact"]
    assert "random-page" not in str(found)


def test_discover_links_caps_blog_at_two():
    html = """
    <a href="/blog/post-1">Blog post 1</a>
    <a href="/blog/post-2">Blog post 2</a>
    <a href="/blog/post-3">Blog post 3</a>
    """
    found = discover_links("https://acme.com", html)
    assert len(found["blog"]) == 2


def test_discover_links_ignores_external_domains():
    html = '<a href="https://otherdomain.com/about">About (not us)</a>'
    found = discover_links("https://acme.com", html)
    assert "about" not in found


def test_discover_links_ignores_common_word_in_long_marketing_sentence():
    # regression: verified live on buffer.com -- a card titled
    # "See how the marketing team plans campaigns with Buffer" linking to an
    # unrelated /made-for/higher-education page was previously misfiled as
    # the "team" page. Long sentence-length link text must not match; only
    # the URL path or short nav-like text should.
    html = '<a href="/made-for/higher-education">See how the marketing team plans campaigns with Buffer</a>'
    found = discover_links("https://acme.com", html)
    assert "team" not in found


def test_discover_links_finds_team_page_via_short_nav_text():
    html = '<a href="/team">Team</a>'
    found = discover_links("https://acme.com", html)
    assert found["team"] == ["https://acme.com/team"]


def test_discover_links_finds_team_page_via_path_even_with_long_text():
    html = '<a href="/leadership">Meet the people leading our company forward every day</a>'
    found = discover_links("https://acme.com", html)
    assert found["team"] == ["https://acme.com/leadership"]


def test_discover_links_dedupes_same_page_different_tracking_params():
    # regression: verified live on buffer.com -- same /insights page linked
    # twice with different ?cta= tracking params burned 2 of the 8-page
    # budget on identical content instead of 1
    html = """
    <a href="/blog?cta=nav-top">Blog</a>
    <a href="/blog?cta=footer-link">News</a>
    """
    found = discover_links("https://acme.com", html)
    assert found["blog"] == ["https://acme.com/blog"]


# --- merge_discovery -----------------------------------------------------------

def test_merge_discovery_tops_up_blog_from_sitemap():
    nav = {"blog": ["https://acme.com/blog/post-1"]}
    sitemap = {"blog": ["https://acme.com/blog/post-1", "https://acme.com/blog/post-2"]}
    merged = merge_discovery(nav, sitemap)
    assert len(merged["blog"]) == 2
    assert "https://acme.com/blog/post-2" in merged["blog"]


def test_merge_discovery_nav_priority_for_single_slot_types():
    nav = {"about": ["https://acme.com/about-us"]}
    sitemap = {"about": ["https://acme.com/about-different"]}
    merged = merge_discovery(nav, sitemap)
    assert merged["about"] == ["https://acme.com/about-us"]


# --- looks_blocked ---------------------------------------------------------------

def test_looks_blocked_detects_cloudflare_wall():
    html = "<title>Just a moment...</title><body>Checking your browser before accessing.</body>"
    assert looks_blocked(html) is True


def test_looks_blocked_false_on_normal_content():
    html = "<body><h1>Welcome to Acme</h1><p>We sell widgets since 1998.</p></body>"
    assert looks_blocked(html) is False


# --- classify_error ----------------------------------------------------------------

def test_classify_error_dns_failure_is_permanent():
    assert classify_error(Exception("net::ERR_NAME_NOT_RESOLVED at https://x.com")) == "permanent"


def test_classify_error_timeout_is_transient():
    assert classify_error(Exception("net::ERR_CONNECTION_TIMED_OUT")) == "transient"


# --- is_cache_fresh --------------------------------------------------------------

def test_is_cache_fresh_true_for_recent_ok_result(tmp_path):
    p = tmp_path / "crawl_result.json"
    p.write_text(json.dumps({
        "status": "ok",
        "crawled_at": datetime.now(timezone.utc).isoformat(),
    }))
    assert is_cache_fresh(p) is True


def test_is_cache_fresh_false_for_old_result(tmp_path):
    p = tmp_path / "crawl_result.json"
    old = datetime.now(timezone.utc) - timedelta(days=60)
    p.write_text(json.dumps({"status": "ok", "crawled_at": old.isoformat()}))
    assert is_cache_fresh(p) is False


def test_is_cache_fresh_false_for_failed_status_recent(tmp_path):
    p = tmp_path / "crawl_result.json"
    p.write_text(json.dumps({
        "status": "crawl_failed",
        "crawled_at": datetime.now(timezone.utc).isoformat(),
    }))
    # a recent FAILED crawl should NOT be treated as cached-fresh -- it should retry
    assert is_cache_fresh(p) is False


def test_is_cache_fresh_false_for_missing_file(tmp_path):
    assert is_cache_fresh(tmp_path / "does_not_exist.json") is False


# --- extract_team_members_from_html (structural, replaces trafilatura for this) ---

def test_extract_team_members_finds_name_and_title_in_card_structure():
    # mirrors the real structure verified live on buffer.com's team page
    html = """
    <div class="card">
      <h3><button>Joel Gascoigne</button></h3>
      <div><p>CEO & Co-Founder</p><p>Chicago, Illinois, USA</p></div>
    </div>
    """
    people = extract_team_members_from_html(html)
    assert {"name": "Joel Gascoigne", "title": "CEO & Co-Founder"} in people


def test_extract_team_members_title_keyword_wins_over_name_shape():
    # regression: verified live on buffer.com -- "Senior Engineer" is shaped
    # like a 2-word name but must be classified as a title, not a person
    html = """
    <h3>Adnan Issadeen</h3><p>Senior Engineer</p>
    """
    people = extract_team_members_from_html(html)
    names = [p["name"] for p in people]
    assert "Senior Engineer" not in names
    assert "Adnan Issadeen" in names


def test_extract_team_members_ignores_stopword_ui_phrases():
    html = "<h3>Get Started</h3><p>Manager</p>"
    people = extract_team_members_from_html(html)
    assert people == []


def test_extract_team_members_empty_for_no_cards():
    html = "<p>We build great products for everyone.</p>"
    assert extract_team_members_from_html(html) == []


# --- extract_jsonld_people (structured data, high precision) ----------------------

def test_extract_jsonld_people_finds_founders():
    # mirrors the real JSON-LD block verified live on stripe.com's homepage
    html = """
    <script type="application/ld+json">
    {"@context":"https://schema.org","@graph":[{"@type":"Organization",
    "name":"Acme","founders":[{"@type":"Person","name":"Patrick Collison"},
    {"@type":"Person","name":"John Collison"}]}]}
    </script>
    """
    people = extract_jsonld_people(html)
    names = {p["name"] for p in people}
    assert names == {"Patrick Collison", "John Collison"}


def test_extract_jsonld_people_uses_job_title_when_present():
    html = """
    <script type="application/ld+json">
    {"@type":"Organization","employees":[{"@type":"Person","name":"Jane Diaz","jobTitle":"Founder"}]}
    </script>
    """
    people = extract_jsonld_people(html)
    assert {"name": "Jane Diaz", "title": "Founder"} in people


def test_extract_jsonld_people_ignores_malformed_json_gracefully():
    html = '<script type="application/ld+json">{not valid json!!!</script>'
    assert extract_jsonld_people(html) == []


def test_extract_jsonld_people_empty_when_no_jsonld_present():
    html = "<p>No structured data here.</p>"
    assert extract_jsonld_people(html) == []


def test_extract_jsonld_people_ignores_non_person_entries():
    html = """
    <script type="application/ld+json">
    {"@type":"Organization","member":[{"@type":"Organization","name":"Some Partner Org"}]}
    </script>
    """
    assert extract_jsonld_people(html) == []


# --- discover_via_sitemap ------------------------------------------------------------

@patch("crawl.requests.get")
def test_discover_via_sitemap_joins_relative_locs_to_absolute(mock_get):
    # regression: verified live on basecamp.com -- its real sitemap.xml uses
    # bare relative paths ("/gettingreal") instead of absolute URLs, which
    # the sitemap spec requires but real sites don't always honor. Before
    # this fix, the bare path leaked straight to the page fetcher and failed.
    mock_get.return_value = MagicMock(status_code=200, text="""
        <urlset><url><loc>/about</loc></url><url><loc>/team</loc></url></urlset>
    """)
    found = discover_via_sitemap("acme.com")
    assert found["about"] == ["https://acme.com/about"]
    assert found["team"] == ["https://acme.com/team"]


@patch("crawl.requests.get")
def test_discover_via_sitemap_leaves_absolute_locs_unchanged(mock_get):
    mock_get.return_value = MagicMock(status_code=200, text="""
        <urlset><url><loc>https://acme.com/about</loc></url></urlset>
    """)
    found = discover_via_sitemap("acme.com")
    assert found["about"] == ["https://acme.com/about"]


@patch("crawl.requests.get")
def test_discover_via_sitemap_handles_request_failure_gracefully(mock_get):
    mock_get.side_effect = Exception("network error")
    assert discover_via_sitemap("acme.com") == {}


@patch("crawl.requests.get")
def test_discover_via_sitemap_skips_non_200_response(mock_get):
    mock_get.return_value = MagicMock(status_code=404, text="")
    assert discover_via_sitemap("acme.com") == {}


# --- extract_clean_text markdown output mode (Firecrawl-format parity) ------------

def test_extract_clean_text_default_is_plain_text():
    html = "<html><body><article><h1>Title</h1><p>Some body text.</p></article></body></html>"
    text = extract_clean_text(html, "https://acme.com")
    assert "# Title" not in text
    assert "Title" in text


def test_extract_clean_text_markdown_mode_preserves_heading_syntax():
    html = "<html><body><article><h1>Title</h1><p>Some body text.</p></article></body></html>"
    text = extract_clean_text(html, "https://acme.com", markdown=True)
    assert "# Title" in text


# --- PDF support (Firecrawl handles PDFs; we didn't at all before this) ----------

def _make_test_pdf_bytes(text: str) -> bytes:
    from fpdf import FPDF
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=12)
    pdf.cell(text=text)
    return bytes(pdf.output())


def test_extract_pdf_text_extracts_readable_text():
    pdf_bytes = _make_test_pdf_bytes("Jane Diaz, Founder and CEO of Acme Corp.")
    text = extract_pdf_text(pdf_bytes)
    assert "Jane Diaz" in text
    assert "Acme Corp" in text


def test_extract_pdf_text_returns_empty_string_on_corrupt_pdf():
    assert extract_pdf_text(b"not a real pdf at all") == ""


def test_is_pdf_url_true_for_pdf_extension():
    assert is_pdf_url("https://acme.com/team-roster.pdf") is True


def test_is_pdf_url_false_for_html_page():
    assert is_pdf_url("https://acme.com/about") is False


def test_is_pdf_url_case_insensitive():
    assert is_pdf_url("https://acme.com/FILE.PDF") is True


def test_is_pdf_url_ignores_query_string():
    assert is_pdf_url("https://acme.com/report.pdf?v=2") is True


@patch("crawl.requests.get")
def test_fetch_pdf_bytes_returns_content_on_success(mock_get):
    mock_get.return_value = MagicMock(status_code=200, content=b"%PDF-1.4 fake pdf bytes")
    result = fetch_pdf_bytes("https://acme.com/team.pdf")
    assert result == b"%PDF-1.4 fake pdf bytes"


@patch("crawl.requests.get")
def test_fetch_pdf_bytes_returns_none_on_non_200(mock_get):
    mock_get.return_value = MagicMock(status_code=404, content=b"")
    assert fetch_pdf_bytes("https://acme.com/missing.pdf") is None


@patch("crawl.requests.get")
def test_fetch_pdf_bytes_returns_none_on_network_error(mock_get):
    mock_get.side_effect = Exception("network error")
    assert fetch_pdf_bytes("https://acme.com/team.pdf") is None


# --- build_context_kwargs (proxy support -- Firecrawl uses distributed IPs) --------

def test_build_context_kwargs_no_proxy_by_default():
    kwargs = build_context_kwargs(proxy_url=None)
    assert "proxy" not in kwargs


def test_build_context_kwargs_includes_proxy_server():
    kwargs = build_context_kwargs(proxy_url="http://proxy.example.com:8080")
    assert kwargs["proxy"]["server"] == "http://proxy.example.com:8080"
    assert "username" not in kwargs["proxy"]


def test_build_context_kwargs_extracts_embedded_credentials():
    kwargs = build_context_kwargs(proxy_url="http://myuser:mypass@proxy.example.com:8080")
    assert kwargs["proxy"]["server"] == "http://proxy.example.com:8080"
    assert kwargs["proxy"]["username"] == "myuser"
    assert kwargs["proxy"]["password"] == "mypass"


def test_build_context_kwargs_still_includes_base_settings_with_proxy():
    kwargs = build_context_kwargs(proxy_url="http://proxy.example.com:8080")
    assert kwargs["ignore_https_errors"] is True
    assert kwargs["locale"] == "en-US"


# --- capture_screenshot (Firecrawl offers screenshots; we had none) ---------------
# Real local Playwright page, no network -- genuine verification, not a mock.

@pytest.fixture(scope="module")
def real_page():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto("data:text/html,<html><body><h1>Hello</h1></body></html>")
        yield page
        browser.close()


def test_capture_screenshot_creates_real_image_file(real_page, tmp_path):
    out_path = tmp_path / "shot.png"
    result = capture_screenshot(real_page, str(out_path))
    assert result is True
    assert out_path.exists()
    assert out_path.stat().st_size > 100  # a real PNG, not an empty/stub file


def test_capture_screenshot_returns_false_on_bad_page_object():
    class BrokenPage:
        def screenshot(self, **kwargs):
            raise Exception("page closed")
    assert capture_screenshot(BrokenPage(), "/tmp/whatever.png") is False
