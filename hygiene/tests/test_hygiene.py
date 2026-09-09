import csv
import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from hygiene import (
    strip_tracking_params,
    normalize_url,
    dedupe_leads,
    has_mx_record,
    is_likely_parked,
    geo_tag_from_tld,
    is_globally_suppressed,
    run_hygiene,
)


# --- strip_tracking_params -----------------------------------------------------

def test_strip_tracking_params_removes_utm():
    url = "https://acme.com/?utm_source=fb&utm_campaign=x&id=5"
    result = strip_tracking_params(url)
    assert "utm_source" not in result
    assert "utm_campaign" not in result
    assert "id=5" in result


def test_strip_tracking_params_removes_common_click_ids():
    url = "https://acme.com/?fbclid=abc&gclid=def"
    result = strip_tracking_params(url)
    assert "fbclid" not in result
    assert "gclid" not in result


def test_strip_tracking_params_leaves_clean_url_unchanged():
    assert strip_tracking_params("https://acme.com/about") == "https://acme.com/about"


# --- normalize_url --------------------------------------------------------------

def test_normalize_url_extracts_root_domain():
    assert normalize_url("https://www.Acme.com/foo?utm_source=x") == "acme.com"


def test_normalize_url_bare_domain():
    assert normalize_url("acme.com") == "acme.com"


def test_normalize_url_subdomain_collapses_to_root():
    assert normalize_url("https://blog.acme.co.uk/post") == "acme.co.uk"


def test_normalize_url_empty_input():
    assert normalize_url("") == ""


def test_normalize_url_garbage_input():
    assert normalize_url("not a url!!") == ""


# --- dedupe_leads ----------------------------------------------------------------

def test_dedupe_leads_keeps_first_occurrence():
    leads = [
        {"domain": "acme.com", "name": "First"},
        {"domain": "acme.com", "name": "Second"},
    ]
    kept, dup_count = dedupe_leads(leads)
    assert len(kept) == 1
    assert kept[0]["name"] == "First"
    assert dup_count == 1


def test_dedupe_leads_no_duplicates():
    leads = [{"domain": "acme.com"}, {"domain": "other.com"}]
    kept, dup_count = dedupe_leads(leads)
    assert len(kept) == 2
    assert dup_count == 0


def test_dedupe_leads_case_insensitive():
    leads = [{"domain": "Acme.com"}, {"domain": "acme.com"}]
    kept, dup_count = dedupe_leads(leads)
    assert len(kept) == 1
    assert dup_count == 1


# --- has_mx_record (mocked DNS) --------------------------------------------------

@patch("hygiene.dns.resolver.resolve")
def test_has_mx_record_true(mock_resolve):
    mock_resolve.return_value = [MagicMock()]
    assert has_mx_record("acme.com") is True


@patch("hygiene.dns.resolver.resolve")
def test_has_mx_record_false_on_nxdomain(mock_resolve):
    import dns.resolver as real_resolver
    mock_resolve.side_effect = real_resolver.NXDOMAIN()
    assert has_mx_record("acme.com") is False


@patch("hygiene.dns.resolver.resolve")
def test_has_mx_record_false_on_no_answer(mock_resolve):
    import dns.resolver as real_resolver
    mock_resolve.side_effect = real_resolver.NoAnswer()
    assert has_mx_record("acme.com") is False


# --- is_likely_parked (mocked DNS) -----------------------------------------------

@patch("hygiene.dns.resolver.resolve")
def test_is_likely_parked_true_for_known_parking_nameserver(mock_resolve):
    ns_record = MagicMock()
    ns_record.target = "ns1.sedoparking.com."
    mock_resolve.return_value = [ns_record]
    assert is_likely_parked("some-parked-domain.com") is True


@patch("hygiene.dns.resolver.resolve")
def test_is_likely_parked_false_for_normal_nameserver(mock_resolve):
    ns_record = MagicMock()
    ns_record.target = "ns1.google.com."
    mock_resolve.return_value = [ns_record]
    assert is_likely_parked("acme.com") is False


@patch("hygiene.dns.resolver.resolve")
def test_is_likely_parked_false_on_lookup_failure(mock_resolve):
    mock_resolve.side_effect = Exception("timeout")
    assert is_likely_parked("acme.com") is False


# --- geo_tag_from_tld --------------------------------------------------------------

def test_geo_tag_canada():
    assert geo_tag_from_tld("acme.ca") == "CA"


def test_geo_tag_uk():
    assert geo_tag_from_tld("acme.co.uk") == "UK"


def test_geo_tag_germany():
    assert geo_tag_from_tld("acme.de") == "DE"


def test_geo_tag_generic_tld_unknown():
    assert geo_tag_from_tld("acme.com") == "unknown"


# --- is_globally_suppressed (real sqlite, sender/db.py schema) --------------------

@pytest.fixture
def suppression_db(tmp_path):
    db_path = tmp_path / "sender_state.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE suppression (
            value TEXT NOT NULL, scope TEXT NOT NULL, reason TEXT,
            suppressed_at TEXT NOT NULL, PRIMARY KEY (value, scope)
        )
    """)
    conn.execute("INSERT INTO suppression VALUES ('acme.com', 'domain', 'unsubscribed', '2026-01-01')")
    conn.commit()
    conn.close()
    return str(db_path)


def test_is_globally_suppressed_true_for_suppressed_domain(suppression_db):
    assert is_globally_suppressed("acme.com", suppression_db) is True


def test_is_globally_suppressed_false_for_clean_domain(suppression_db):
    assert is_globally_suppressed("other.com", suppression_db) is False


def test_is_globally_suppressed_false_when_no_db_file(tmp_path):
    assert is_globally_suppressed("acme.com", str(tmp_path / "does_not_exist.db")) is False


def test_is_globally_suppressed_false_when_no_path_given():
    assert is_globally_suppressed("acme.com", None) is False


# --- run_hygiene (full pipeline, integration) -------------------------------------

@patch("hygiene.is_likely_parked", return_value=False)
@patch("hygiene.has_mx_record", return_value=True)
def test_run_hygiene_end_to_end_writes_kept_and_dropped(mock_mx, mock_parked, tmp_path):
    in_csv = tmp_path / "leads.csv"
    with open(in_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["company", "website"])
        w.writeheader()
        w.writerow({"company": "Acme", "website": "https://www.acme.com/?utm_source=x"})
        w.writerow({"company": "Acme Dup", "website": "acme.com"})  # duplicate
        w.writerow({"company": "Other", "website": "other.co.uk"})

    kept_path = tmp_path / "kept.csv"
    dropped_path = tmp_path / "dropped.csv"
    summary = run_hygiene(str(in_csv), "website", str(kept_path), str(dropped_path))

    assert summary["total_input"] == 3
    assert summary["duplicates_dropped"] == 1
    assert summary["kept"] == 2

    with open(kept_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 2
    domains = {r["website"] for r in rows}
    assert domains == {"acme.com", "other.co.uk"}
    # crawler.py compatibility: output column must be named "website" (its
    # own --domain-column default) so the hand-off needs zero reformatting
    assert "website" in rows[0]
    geo_by_domain = {r["website"]: r["geo"] for r in rows}
    assert geo_by_domain["other.co.uk"] == "UK"


@patch("hygiene.is_likely_parked", return_value=False)
@patch("hygiene.has_mx_record", return_value=False)
def test_run_hygiene_drops_domains_without_mx(mock_mx, mock_parked, tmp_path):
    in_csv = tmp_path / "leads.csv"
    with open(in_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["website"])
        w.writeheader()
        w.writerow({"website": "acme.com"})

    kept_path = tmp_path / "kept.csv"
    dropped_path = tmp_path / "dropped.csv"
    summary = run_hygiene(str(in_csv), "website", str(kept_path), str(dropped_path))

    assert summary["kept"] == 0
    assert summary["no_mx_dropped"] == 1
    with open(dropped_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert rows[0]["drop_reason"] == "no_mx"


@patch("hygiene.is_likely_parked", return_value=True)
@patch("hygiene.has_mx_record", return_value=True)
def test_run_hygiene_drops_parked_domains(mock_mx, mock_parked, tmp_path):
    in_csv = tmp_path / "leads.csv"
    with open(in_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["website"])
        w.writeheader()
        w.writerow({"website": "parked-example.com"})

    kept_path = tmp_path / "kept.csv"
    dropped_path = tmp_path / "dropped.csv"
    summary = run_hygiene(str(in_csv), "website", str(kept_path), str(dropped_path))

    assert summary["parked_dropped"] == 1


@patch("hygiene.is_likely_parked", return_value=False)
@patch("hygiene.has_mx_record", return_value=True)
def test_run_hygiene_drops_suppressed_domains(mock_mx, mock_parked, tmp_path, suppression_db):
    in_csv = tmp_path / "leads.csv"
    with open(in_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["website"])
        w.writeheader()
        w.writerow({"website": "acme.com"})  # matches suppression_db fixture

    kept_path = tmp_path / "kept.csv"
    dropped_path = tmp_path / "dropped.csv"
    summary = run_hygiene(str(in_csv), "website", str(kept_path), str(dropped_path),
                           sender_db_path=suppression_db)

    assert summary["suppressed_dropped"] == 1
    assert summary["kept"] == 0


def test_run_hygiene_missing_column_raises_clear_error(tmp_path):
    in_csv = tmp_path / "leads.csv"
    with open(in_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["company"])
        w.writeheader()
        w.writerow({"company": "Acme"})

    with pytest.raises(SystemExit):
        run_hygiene(str(in_csv), "website", str(tmp_path / "kept.csv"), str(tmp_path / "dropped.csv"))
