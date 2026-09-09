import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from manual_gaps import (
    is_gap,
    company_name_guess,
    google_search_url,
    build_gap_row,
    find_gap_domains,
    export_gaps_csv,
    import_manual_csv,
)


# --- is_gap -------------------------------------------------------------------

def test_is_gap_true_for_no_contacts_found():
    is_it, reason = is_gap({"status": "no_contacts_found", "contacts": []})
    assert is_it is True
    assert reason == "no_contact"


def test_is_gap_true_for_no_crawl_data():
    is_it, reason = is_gap({"status": "no_crawl_data", "contacts": []})
    assert is_it is True
    assert reason == "no_contact"


def test_is_gap_true_when_all_contacts_low_confidence():
    data = {"status": "ok", "contacts": [{"confidence": "low"}, {"confidence": "low"}]}
    is_it, reason = is_gap(data)
    assert is_it is True
    assert reason == "low_confidence"


def test_is_gap_false_when_any_contact_medium_or_high():
    data = {"status": "ok", "contacts": [{"confidence": "low"}, {"confidence": "medium"}]}
    is_it, reason = is_gap(data)
    assert is_it is False


def test_is_gap_false_for_high_confidence_contact():
    data = {"status": "ok", "contacts": [{"confidence": "high"}]}
    assert is_gap(data) == (False, "")


# --- company_name_guess / google_search_url ---------------------------------

def test_company_name_guess_title_cases_domain():
    assert company_name_guess("stripe.com") == "Stripe"


def test_company_name_guess_handles_hyphenated_domain():
    assert company_name_guess("acme-widgets.com") == "Acme Widgets"


def test_google_search_url_builds_valid_link():
    url = google_search_url("Acme Corp founder CEO")
    assert url.startswith("https://www.google.com/search?q=")
    assert "Acme" in url or "%22Acme%22" in url or "Acme+Corp" in url


# --- build_gap_row -------------------------------------------------------------

def test_build_gap_row_includes_search_links_and_blank_fields():
    row = build_gap_row("acme.com", {"status": "no_contacts_found", "contacts": []}, "no_contact")
    assert row["domain"] == "acme.com"
    assert row["company_name_guess"] == "Acme"
    assert row["gap_reason"] == "no_contact"
    assert "google.com/search" in row["google_search_url"]
    assert "linkedin.com" in row["linkedin_search_url"]
    assert row["found_name"] == ""
    assert row["found_email"] == ""


def test_build_gap_row_surfaces_existing_low_confidence_guess():
    data = {"status": "ok", "contacts": [{"email": "info@acme.com", "confidence": "low", "matched_name": None}]}
    row = build_gap_row("acme.com", data, "low_confidence")
    assert row["existing_best_email"] == "info@acme.com"


# --- find_gap_domains / export_gaps_csv / import_manual_csv (integration) ---------

@pytest.fixture
def crawl_dir(tmp_path):
    d = tmp_path / "output"
    d.mkdir()
    return d


def _write_contacts(crawl_dir, domain, data):
    domain_dir = crawl_dir / domain
    domain_dir.mkdir(parents=True, exist_ok=True)
    (domain_dir / "contacts.json").write_text(json.dumps(data), encoding="utf-8")


def test_find_gap_domains_includes_only_gaps(crawl_dir):
    _write_contacts(crawl_dir, "gap.com", {"status": "no_contacts_found", "contacts": []})
    _write_contacts(crawl_dir, "solved.com", {"status": "ok", "contacts": [{"confidence": "high"}]})
    gaps = find_gap_domains(crawl_dir)
    domains = [g[0] for g in gaps]
    assert "gap.com" in domains
    assert "solved.com" not in domains


def test_export_gaps_csv_writes_expected_rows(crawl_dir, tmp_path):
    _write_contacts(crawl_dir, "gap.com", {"status": "no_contacts_found", "contacts": []})
    out_path = tmp_path / "gaps.csv"
    count = export_gaps_csv(crawl_dir, out_path)
    assert count == 1
    with open(out_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert rows[0]["domain"] == "gap.com"


def test_import_manual_csv_adds_contact_to_existing_json(crawl_dir, tmp_path):
    _write_contacts(crawl_dir, "gap.com", {"status": "no_contacts_found", "contacts": []})
    in_path = tmp_path / "filled.csv"
    with open(in_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["domain", "found_name", "found_title", "found_email", "verified_yn"])
        w.writeheader()
        w.writerow({"domain": "gap.com", "found_name": "Jane Diaz", "found_title": "Founder",
                    "found_email": "jane@gap.com", "verified_yn": "Y"})
    summary = import_manual_csv(crawl_dir, in_path)
    assert summary["imported"] == 1

    updated = json.loads((crawl_dir / "gap.com" / "contacts.json").read_text(encoding="utf-8"))
    match = next(c for c in updated["contacts"] if c["email"] == "jane@gap.com")
    assert match["matched_name"] == "Jane Diaz"
    assert match["source"] == "manual_lookup"
    assert match["confidence"] == "high"
    assert updated["status"] == "ok"


def test_import_manual_csv_skips_blank_rows(crawl_dir, tmp_path):
    _write_contacts(crawl_dir, "gap.com", {"status": "no_contacts_found", "contacts": []})
    in_path = tmp_path / "filled.csv"
    with open(in_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["domain", "found_name", "found_title", "found_email", "verified_yn"])
        w.writeheader()
        w.writerow({"domain": "gap.com", "found_name": "", "found_title": "", "found_email": "", "verified_yn": ""})
    summary = import_manual_csv(crawl_dir, in_path)
    assert summary["imported"] == 0
    assert summary["skipped_blank"] == 1


def test_import_manual_csv_unverified_gets_medium_confidence(crawl_dir, tmp_path):
    _write_contacts(crawl_dir, "gap.com", {"status": "no_contacts_found", "contacts": []})
    in_path = tmp_path / "filled.csv"
    with open(in_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["domain", "found_name", "found_title", "found_email", "verified_yn"])
        w.writeheader()
        w.writerow({"domain": "gap.com", "found_name": "Jane Diaz", "found_title": "Founder",
                    "found_email": "jane@gap.com", "verified_yn": "N"})
    import_manual_csv(crawl_dir, in_path)
    updated = json.loads((crawl_dir / "gap.com" / "contacts.json").read_text(encoding="utf-8"))
    match = next(c for c in updated["contacts"] if c["email"] == "jane@gap.com")
    assert match["confidence"] == "medium"
