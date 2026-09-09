import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import db as db_module
import pytest
from reports import campaign_summary, mailbox_breakdown, daily_send_counts


@pytest.fixture
def conn(tmp_path):
    c = db_module.get_connection(str(tmp_path / "test.db"))
    db_module.init_db(c)
    yield c
    c.close()


# --- campaign_summary ----------------------------------------------------------

def test_campaign_summary_counts_leads_by_status(conn):
    db_module.upsert_lead(conn, "a@x.com")
    db_module.upsert_lead(conn, "b@x.com")
    db_module.mark_replied(conn, "b@x.com")
    summary = campaign_summary(conn)
    assert summary["total_leads"] == 2
    assert summary["lead_status_breakdown"]["active"] == 1
    assert summary["lead_status_breakdown"]["replied"] == 1


def test_campaign_summary_computes_reply_rate(conn):
    db_module.upsert_lead(conn, "a@x.com")
    db_module.mark_sent(conn, "a@x.com", 1, "subj", "<mid1>", "mb@x.com")
    db_module.upsert_lead(conn, "b@x.com")
    db_module.mark_sent(conn, "b@x.com", 1, "subj", "<mid2>", "mb@x.com")
    db_module.mark_replied(conn, "b@x.com")

    summary = campaign_summary(conn)
    assert summary["total_sent"] == 2
    assert summary["replied"] == 1
    assert summary["reply_rate"] == 0.5


def test_campaign_summary_computes_bounce_rate(conn):
    db_module.upsert_lead(conn, "a@x.com")
    db_module.mark_sent(conn, "a@x.com", 1, "subj", "<mid1>", "mb@x.com")
    db_module.mark_bounced(conn, "a@x.com")
    summary = campaign_summary(conn)
    assert summary["bounced"] == 1
    assert summary["bounce_rate"] == 1.0


def test_campaign_summary_zero_sent_no_division_error(conn):
    summary = campaign_summary(conn)
    assert summary["total_sent"] == 0
    assert summary["reply_rate"] == 0
    assert summary["bounce_rate"] == 0


def test_campaign_summary_counts_failed_sends_separately(conn):
    db_module.upsert_lead(conn, "a@x.com")
    db_module.mark_send_failed(conn, "a@x.com", 1, "smtp error", "mb@x.com")
    summary = campaign_summary(conn)
    assert summary["total_failed"] == 1
    assert summary["total_sent"] == 0


# --- mailbox_breakdown -----------------------------------------------------------

def test_mailbox_breakdown_groups_by_mailbox(conn):
    db_module.upsert_lead(conn, "a@x.com")
    db_module.mark_sent(conn, "a@x.com", 1, "subj", "<mid1>", "mb1@x.com")
    db_module.upsert_lead(conn, "b@x.com")
    db_module.mark_sent(conn, "b@x.com", 1, "subj", "<mid2>", "mb2@x.com")
    db_module.upsert_lead(conn, "c@x.com")
    db_module.mark_sent(conn, "c@x.com", 1, "subj", "<mid3>", "mb1@x.com")

    breakdown = {row["mailbox"]: row["sent"] for row in mailbox_breakdown(conn)}
    assert breakdown["mb1@x.com"] == 2
    assert breakdown["mb2@x.com"] == 1


def test_mailbox_breakdown_counts_failures(conn):
    db_module.upsert_lead(conn, "a@x.com")
    db_module.mark_send_failed(conn, "a@x.com", 1, "err", "mb1@x.com")
    breakdown = {row["mailbox"]: row["failed"] for row in mailbox_breakdown(conn)}
    assert breakdown["mb1@x.com"] == 1


# --- daily_send_counts -------------------------------------------------------------

def test_daily_send_counts_aggregates_across_mailboxes(conn):
    db_module.increment_mailbox_count(conn, "mb1@x.com", "2026-01-01")
    db_module.increment_mailbox_count(conn, "mb2@x.com", "2026-01-01")
    db_module.increment_mailbox_count(conn, "mb1@x.com", "2026-01-02")

    counts = {row["day"]: row["total"] for row in daily_send_counts(conn)}
    assert counts["2026-01-01"] == 2
    assert counts["2026-01-02"] == 1


def test_daily_send_counts_respects_limit(conn):
    for i in range(20):
        db_module.increment_mailbox_count(conn, "mb@x.com", f"2026-01-{i+1:02d}")
    counts = daily_send_counts(conn, days=5)
    assert len(counts) == 5
