import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import db as db_module
import pytest


@pytest.fixture
def conn(tmp_path):
    c = db_module.get_connection(str(tmp_path / "test.db"))
    db_module.init_db(c)
    yield c
    c.close()


def test_upsert_lead_creates_new_lead(conn):
    db_module.upsert_lead(conn, "jane@acme.com", name="Jane Diaz")
    lead = db_module.get_lead(conn, "jane@acme.com")
    assert lead["email"] == "jane@acme.com"
    assert lead["name"] == "Jane Diaz"
    assert lead["status"] == "active"
    assert lead["sequence_step"] == 0


def test_upsert_lead_idempotent_does_not_overwrite(conn):
    db_module.upsert_lead(conn, "jane@acme.com", name="Jane Diaz")
    db_module.mark_sent(conn, "jane@acme.com", 1, "subj", "<msgid>", "mailbox@x.com")
    db_module.upsert_lead(conn, "jane@acme.com", name="Different Name")
    lead = db_module.get_lead(conn, "jane@acme.com")
    # second upsert must NOT reset progress already made
    assert lead["sequence_step"] == 1
    assert lead["name"] == "Jane Diaz"


def test_email_case_normalized(conn):
    db_module.upsert_lead(conn, "Jane@ACME.com")
    assert db_module.get_lead(conn, "jane@acme.com") is not None


# --- suppression --------------------------------------------------------------

def test_suppression_by_exact_email(conn):
    db_module.add_suppression(conn, "jane@acme.com", "email", "unsubscribed")
    assert db_module.is_suppressed(conn, "jane@acme.com") is True
    assert db_module.is_suppressed(conn, "john@acme.com") is False


def test_suppression_by_domain_blocks_all_addresses_at_domain(conn):
    db_module.add_suppression(conn, "acme.com", "domain", "unsubscribed (colleague)")
    assert db_module.is_suppressed(conn, "anyone@acme.com") is True
    assert db_module.is_suppressed(conn, "someone@other.com") is False


def test_mark_unsubscribed_suppresses_domain_wide_by_default(conn):
    db_module.upsert_lead(conn, "jane@acme.com")
    db_module.upsert_lead(conn, "john@acme.com")
    db_module.mark_unsubscribed(conn, "jane@acme.com")
    # jane herself AND her colleague at the same domain are now suppressed
    assert db_module.is_suppressed(conn, "jane@acme.com") is True
    assert db_module.is_suppressed(conn, "john@acme.com") is True


def test_mark_bounced_suppresses_only_that_email_not_whole_domain(conn):
    db_module.upsert_lead(conn, "jane@acme.com")
    db_module.upsert_lead(conn, "john@acme.com")
    db_module.mark_bounced(conn, "jane@acme.com")
    assert db_module.is_suppressed(conn, "jane@acme.com") is True
    assert db_module.is_suppressed(conn, "john@acme.com") is False  # a bounce isn't "they said no"


def test_mark_replied_does_not_suppress_domain(conn):
    db_module.upsert_lead(conn, "jane@acme.com")
    db_module.upsert_lead(conn, "john@acme.com")
    db_module.mark_replied(conn, "jane@acme.com")
    assert db_module.get_lead(conn, "jane@acme.com")["status"] == "replied"
    assert db_module.is_suppressed(conn, "john@acme.com") is False  # a reply isn't "stop contacting us"


# --- get_leads_due_for_send ------------------------------------------------------

def test_leads_due_for_send_excludes_suppressed(conn):
    db_module.upsert_lead(conn, "jane@acme.com")
    db_module.add_suppression(conn, "jane@acme.com", "email", "manual")
    due = db_module.get_leads_due_for_send(conn)
    assert all(r["email"] != "jane@acme.com" for r in due)


def test_leads_due_for_send_excludes_inactive_status(conn):
    db_module.upsert_lead(conn, "jane@acme.com")
    db_module.mark_replied(conn, "jane@acme.com")
    due = db_module.get_leads_due_for_send(conn)
    assert all(r["email"] != "jane@acme.com" for r in due)


def test_leads_due_for_send_includes_never_sent_lead(conn):
    db_module.upsert_lead(conn, "jane@acme.com")
    due = db_module.get_leads_due_for_send(conn)
    assert any(r["email"] == "jane@acme.com" for r in due)


def test_leads_due_for_send_excludes_future_next_send_at(conn):
    db_module.upsert_lead(conn, "jane@acme.com")
    db_module.reschedule(conn, "jane@acme.com", "2099-01-01T00:00:00+00:00")
    due = db_module.get_leads_due_for_send(conn)
    assert all(r["email"] != "jane@acme.com" for r in due)


# --- mailbox daily counts -------------------------------------------------------

def test_mailbox_count_starts_at_zero(conn):
    assert db_module.get_mailbox_count_today(conn, "me@x.com", "2026-01-01") == 0


def test_mailbox_count_increments(conn):
    db_module.increment_mailbox_count(conn, "me@x.com", "2026-01-01")
    db_module.increment_mailbox_count(conn, "me@x.com", "2026-01-01")
    assert db_module.get_mailbox_count_today(conn, "me@x.com", "2026-01-01") == 2


def test_mailbox_count_isolated_per_day(conn):
    db_module.increment_mailbox_count(conn, "me@x.com", "2026-01-01")
    assert db_module.get_mailbox_count_today(conn, "me@x.com", "2026-01-02") == 0


# --- processed_replies dedup -----------------------------------------------------

def test_reply_dedup_tracking(conn):
    assert db_module.is_reply_processed(conn, "uid123") is False
    db_module.mark_reply_processed(conn, "uid123", "bounce")
    assert db_module.is_reply_processed(conn, "uid123") is True


# --- mark_completed --------------------------------------------------------------

def test_mark_completed(conn):
    db_module.upsert_lead(conn, "jane@acme.com")
    db_module.mark_completed(conn, "jane@acme.com")
    assert db_module.get_lead(conn, "jane@acme.com")["status"] == "completed"


# --- replies table (Stage 10 feedback loop foundation) ----------------------------

def test_save_reply_returns_id(conn):
    reply_id = db_module.save_reply(conn, "jane@acme.com", 1, "Re: quick note", "Sounds interesting, tell me more.")
    assert isinstance(reply_id, int)


def test_get_unclassified_replies_returns_saved_reply(conn):
    db_module.save_reply(conn, "jane@acme.com", 1, "Re: quick note", "Sounds interesting, tell me more.")
    unclassified = db_module.get_unclassified_replies(conn)
    assert len(unclassified) == 1
    assert unclassified[0]["email"] == "jane@acme.com"
    assert unclassified[0]["body_text"] == "Sounds interesting, tell me more."


def test_update_reply_sentiment_removes_from_unclassified(conn):
    reply_id = db_module.save_reply(conn, "jane@acme.com", 1, "Re: quick note", "Sounds interesting.")
    db_module.update_reply_sentiment(conn, reply_id, "positive", confidence=9)
    assert db_module.get_unclassified_replies(conn) == []


def test_get_replies_by_sentiment(conn):
    id1 = db_module.save_reply(conn, "jane@acme.com", 1, "Re: a", "Yes let's talk.")
    id2 = db_module.save_reply(conn, "john@acme.com", 1, "Re: b", "Not interested, remove me.")
    db_module.update_reply_sentiment(conn, id1, "positive", confidence=9)
    db_module.update_reply_sentiment(conn, id2, "negative", confidence=8)

    positive = db_module.get_replies_by_sentiment(conn, "positive")
    assert len(positive) == 1
    assert positive[0]["email"] == "jane@acme.com"


def test_save_reply_stores_step_and_subject(conn):
    db_module.save_reply(conn, "jane@acme.com", 3, "Re: proof point", "body text")
    reply = db_module.get_unclassified_replies(conn)[0]
    assert reply["step"] == 3
    assert reply["subject"] == "Re: proof point"


# --- meeting-booked tracking (manual tag -- no calendar API integration) ----------

def test_mark_meeting_booked(conn):
    db_module.upsert_lead(conn, "jane@acme.com")
    db_module.mark_meeting_booked(conn, "jane@acme.com")
    lead = db_module.get_lead(conn, "jane@acme.com")
    assert lead["meeting_booked_at"] is not None


def test_mark_meeting_booked_does_not_change_status(conn):
    # a meeting is tracked as its own signal, independent of sequence status
    # (the lead might still show "replied" -- meeting_booked is additive)
    db_module.upsert_lead(conn, "jane@acme.com")
    db_module.mark_replied(conn, "jane@acme.com")
    db_module.mark_meeting_booked(conn, "jane@acme.com")
    lead = db_module.get_lead(conn, "jane@acme.com")
    assert lead["status"] == "replied"
    assert lead["meeting_booked_at"] is not None


def test_schema_migration_safe_on_existing_db_without_new_column(tmp_path):
    # simulates a DB created by an OLDER version of this code (before
    # meeting_booked_at existed) -- init_db must add the column without
    # erroring, so upgrading doesn't break an existing production DB
    import sqlite3
    db_path = str(tmp_path / "old.db")
    old_conn = sqlite3.connect(db_path)
    old_conn.execute("""
        CREATE TABLE leads (
            email TEXT PRIMARY KEY, domain TEXT, name TEXT,
            sequence_step INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'active',
            last_sent_at TEXT, next_send_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )
    """)
    old_conn.commit()
    old_conn.close()

    new_conn = db_module.get_connection(db_path)
    db_module.init_db(new_conn)  # must not raise
    db_module.upsert_lead(new_conn, "jane@acme.com")
    db_module.mark_meeting_booked(new_conn, "jane@acme.com")
    assert db_module.get_lead(new_conn, "jane@acme.com")["meeting_booked_at"] is not None
    new_conn.close()
