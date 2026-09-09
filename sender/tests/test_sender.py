import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import db as db_module
import pytest
from config import Config
from mailboxes import Mailbox
import sender


@pytest.fixture
def conn(tmp_path):
    c = db_module.get_connection(str(tmp_path / "test.db"))
    db_module.init_db(c)
    yield c
    c.close()


@pytest.fixture(autouse=True)
def fast_delays(monkeypatch):
    # don't actually sleep 60-180s between sends during tests
    monkeypatch.setattr(Config, "MIN_SEND_DELAY_SEC", 0)
    monkeypatch.setattr(Config, "MAX_SEND_DELAY_SEC", 0)
    monkeypatch.setattr(Config, "MAILBOX_NAME", "me@acme.com")
    # send itself is mocked in these tests, but run_batch still calls
    # require_send_ready() as a guard before it -- give it dummy creds so
    # that check passes and doesn't block testing the orchestration logic
    monkeypatch.setattr(Config, "SMTP_USER", "me@acme.com")
    monkeypatch.setattr(Config, "SMTP_PASSWORD", "dummy")
    monkeypatch.setattr(Config, "PHYSICAL_ADDRESS", "123 Main St")


def _queue(*items):
    return list(items)


# --- dry run -------------------------------------------------------------------

def test_dry_run_sends_nothing_but_reports_would_send(conn):
    queue = _queue({"to_email": "jane@acme.com", "subject": "hi", "body": "b", "step": 1})
    summary = sender.run_batch(queue, conn, dry_run=True)
    assert summary["sent"] == 1
    # dry run must not touch send_log / advance sequence at all
    lead = db_module.get_lead(conn, "jane@acme.com")
    assert lead["sequence_step"] == 0


# --- suppression / status gating ------------------------------------------------

def test_skips_suppressed_lead(conn):
    db_module.add_suppression(conn, "jane@acme.com", "email", "manual")
    queue = _queue({"to_email": "jane@acme.com", "subject": "hi", "body": "b", "step": 1})
    summary = sender.run_batch(queue, conn, dry_run=True)
    assert summary["skipped_suppressed"] == 1
    assert summary["sent"] == 0


def test_skips_inactive_lead(conn):
    db_module.upsert_lead(conn, "jane@acme.com")
    db_module.mark_replied(conn, "jane@acme.com")
    queue = _queue({"to_email": "jane@acme.com", "subject": "hi", "body": "b", "step": 2})
    summary = sender.run_batch(queue, conn, dry_run=True)
    assert summary["skipped_inactive"] == 1


def test_skips_out_of_order_step(conn):
    db_module.upsert_lead(conn, "jane@acme.com")  # sequence_step starts at 0
    queue = _queue({"to_email": "jane@acme.com", "subject": "hi", "body": "b", "step": 3})
    summary = sender.run_batch(queue, conn, dry_run=True)
    assert summary["skipped_out_of_order"] == 1
    assert summary["sent"] == 0


# --- real send path (mocked SMTP) -----------------------------------------------

@patch("sender.smtp_sender.send_email")
def test_successful_send_advances_sequence_and_schedules_next(mock_send, conn):
    mock_send.return_value = (True, "<msg1@acme.com>")
    queue = _queue({"to_email": "jane@acme.com", "subject": "hi", "body": "b", "step": 1})

    summary = sender.run_batch(queue, conn, dry_run=False)

    assert summary["sent"] == 1
    lead = db_module.get_lead(conn, "jane@acme.com")
    assert lead["sequence_step"] == 1
    assert lead["status"] == "active"
    assert lead["next_send_at"] is not None  # scheduled for step 2 in a few days


@patch("sender.smtp_sender.send_email")
def test_send_failure_does_not_advance_sequence(mock_send, conn):
    mock_send.return_value = (False, "smtp error")
    queue = _queue({"to_email": "jane@acme.com", "subject": "hi", "body": "b", "step": 1})

    summary = sender.run_batch(queue, conn, dry_run=False)

    assert summary["failed"] == 1
    lead = db_module.get_lead(conn, "jane@acme.com")
    assert lead["sequence_step"] == 0  # failure must not advance the sequence


@patch("sender.smtp_sender.send_email")
def test_final_step_marks_lead_completed(mock_send, conn):
    mock_send.return_value = (True, "<msg@acme.com>")
    db_module.upsert_lead(conn, "jane@acme.com")
    # fast-forward to step 3 already sent, so this send is step 4 (the last)
    conn.execute("UPDATE leads SET sequence_step = 3 WHERE email = 'jane@acme.com'")
    conn.commit()

    queue = _queue({"to_email": "jane@acme.com", "subject": "hi", "body": "b", "step": 4})
    sender.run_batch(queue, conn, dry_run=False)

    lead = db_module.get_lead(conn, "jane@acme.com")
    assert lead["status"] == "completed"


@patch("sender.smtp_sender.send_email")
def test_followup_threads_as_reply_to_step_1(mock_send, conn):
    mock_send.return_value = (True, "<msg1@acme.com>")
    sender.run_batch(_queue({"to_email": "jane@acme.com", "subject": "hi", "body": "b1", "step": 1}), conn)

    mock_send.return_value = (True, "<msg2@acme.com>")
    sender.run_batch(_queue({"to_email": "jane@acme.com", "subject": "hi", "body": "b2", "step": 2}), conn)

    # the second call to send_email should have been given step-1's message-id
    # as in_reply_to/references
    _, kwargs = mock_send.call_args
    assert kwargs["in_reply_to"] == "<msg1@acme.com>"


@patch("sender.smtp_sender.send_email")
def test_send_email_receives_list_unsubscribe_mailto(mock_send, conn, monkeypatch):
    mock_send.return_value = (True, "<msg@acme.com>")
    monkeypatch.setattr(Config, "REPLY_TO", "me@acme.com")
    sender.run_batch(_queue({"to_email": "jane@acme.com", "subject": "hi", "body": "b", "step": 1}), conn)
    _, kwargs = mock_send.call_args
    assert kwargs["unsubscribe_mailto"] == "me@acme.com"


# --- daily cap enforcement -------------------------------------------------------

@patch("sender.smtp_sender.send_email")
def test_stops_at_daily_cap(mock_send, conn, monkeypatch):
    mock_send.return_value = (True, "<msg@acme.com>")
    monkeypatch.setattr(Config, "DAILY_CAP_OVERRIDE", 2)

    queue = _queue(
        {"to_email": "a@acme.com", "subject": "hi", "body": "b", "step": 1},
        {"to_email": "b@acme.com", "subject": "hi", "body": "b", "step": 1},
        {"to_email": "c@acme.com", "subject": "hi", "body": "b", "step": 1},
    )
    summary = sender.run_batch(queue, conn, dry_run=False)

    assert summary["sent"] == 2
    assert summary["skipped_cap_reached"] >= 1
    # the third lead must not have been sent to at all
    assert db_module.get_lead(conn, "c@acme.com") is None or db_module.get_lead(conn, "c@acme.com")["sequence_step"] == 0


# --- limit flag (separate from daily cap) ---------------------------------------

@patch("sender.smtp_sender.send_email")
def test_limit_flag_caps_this_invocation(mock_send, conn, monkeypatch):
    mock_send.return_value = (True, "<msg@acme.com>")
    monkeypatch.setattr(Config, "DAILY_CAP_OVERRIDE", 100)

    queue = _queue(
        {"to_email": "a@acme.com", "subject": "hi", "body": "b", "step": 1},
        {"to_email": "b@acme.com", "subject": "hi", "body": "b", "step": 1},
    )
    summary = sender.run_batch(queue, conn, dry_run=False, limit=1)
    assert summary["sent"] == 1


# --- multi-mailbox rotation --------------------------------------------------------

def _pool_mb(name, daily_cap_override=10):
    from datetime import date
    return Mailbox(
        name=name, smtp_host="smtp.x.com", smtp_port=587, smtp_user=name, smtp_password="pw",
        imap_host="imap.x.com", imap_port=993, from_name="Name", reply_to=name,
        warmup_start_date=date(2020, 1, 1), daily_cap_override=daily_cap_override,
    )


@patch("sender.smtp_sender.send_email")
def test_multi_mailbox_distributes_sends_across_pool(mock_send, conn):
    mock_send.return_value = (True, "<msg@x.com>")
    pool = [_pool_mb("a@x.com", daily_cap_override=1), _pool_mb("b@x.com", daily_cap_override=1)]

    queue = _queue(
        {"to_email": "lead1@acme.com", "subject": "hi", "body": "b", "step": 1},
        {"to_email": "lead2@acme.com", "subject": "hi", "body": "b", "step": 1},
    )
    summary = sender.run_batch(queue, conn, mailboxes=pool, dry_run=False)

    assert summary["sent"] == 2
    # each mailbox has a cap of 1 -- both must have been used, one each
    today = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).date().isoformat()
    assert db_module.get_mailbox_count_today(conn, "a@x.com", today) == 1
    assert db_module.get_mailbox_count_today(conn, "b@x.com", today) == 1


@patch("sender.smtp_sender.send_email")
def test_multi_mailbox_stops_only_when_all_at_cap(mock_send, conn):
    mock_send.return_value = (True, "<msg@x.com>")
    pool = [_pool_mb("a@x.com", daily_cap_override=1), _pool_mb("b@x.com", daily_cap_override=1)]

    queue = _queue(
        {"to_email": "lead1@acme.com", "subject": "hi", "body": "b", "step": 1},
        {"to_email": "lead2@acme.com", "subject": "hi", "body": "b", "step": 1},
        {"to_email": "lead3@acme.com", "subject": "hi", "body": "b", "step": 1},
    )
    summary = sender.run_batch(queue, conn, mailboxes=pool, dry_run=False)

    assert summary["sent"] == 2  # only 2 total capacity across the whole pool
    assert summary["skipped_cap_reached"] >= 1


@patch("sender.smtp_sender.send_email")
def test_multi_mailbox_send_failure_uses_correct_mailbox_name_in_log(mock_send, conn):
    mock_send.return_value = (False, "smtp error")
    pool = [_pool_mb("a@x.com", daily_cap_override=5)]

    queue = _queue({"to_email": "lead1@acme.com", "subject": "hi", "body": "b", "step": 1})
    sender.run_batch(queue, conn, mailboxes=pool, dry_run=False)

    row = conn.execute("SELECT mailbox FROM send_log WHERE email = 'lead1@acme.com'").fetchone()
    assert row["mailbox"] == "a@x.com"


def test_multi_mailbox_dry_run_does_not_touch_daily_counts(conn):
    pool = [_pool_mb("a@x.com", daily_cap_override=5)]
    queue = _queue({"to_email": "lead1@acme.com", "subject": "hi", "body": "b", "step": 1})
    sender.run_batch(queue, conn, mailboxes=pool, dry_run=True)

    today = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).date().isoformat()
    assert db_module.get_mailbox_count_today(conn, "a@x.com", today) == 0


# --- run_preflight_check -----------------------------------------------------------

@patch("sender.deliverability.run_health_check")
def test_preflight_warns_on_missing_spf(mock_check, caplog):
    mock_check.return_value = {"spf": {"found": False}, "dmarc": {"found": True, "policy": "quarantine"},
                                "dkim_selectors_found": []}
    pool = [_pool_mb("a@acme.com")]
    with caplog.at_level("WARNING"):
        sender.run_preflight_check(pool)
    assert any("SPF" in r.message for r in caplog.records)


@patch("sender.deliverability.run_health_check")
def test_preflight_warns_on_missing_dmarc(mock_check, caplog):
    mock_check.return_value = {"spf": {"found": True}, "dmarc": {"found": False, "policy": None},
                                "dkim_selectors_found": []}
    pool = [_pool_mb("a@acme.com")]
    with caplog.at_level("WARNING"):
        sender.run_preflight_check(pool)
    assert any("DMARC" in r.message for r in caplog.records)


@patch("sender.deliverability.run_health_check")
def test_preflight_silent_when_all_good(mock_check, caplog):
    mock_check.return_value = {"spf": {"found": True}, "dmarc": {"found": True, "policy": "quarantine"},
                                "dkim_selectors_found": ["google"]}
    pool = [_pool_mb("a@acme.com")]
    with caplog.at_level("WARNING"):
        sender.run_preflight_check(pool)
    assert len(caplog.records) == 0


def test_preflight_skipped_when_flag_set(caplog):
    pool = [_pool_mb("a@acme.com")]
    with caplog.at_level("WARNING"), patch("sender.deliverability.run_health_check") as mock_check:
        sender.run_preflight_check(pool, skip=True)
        mock_check.assert_not_called()
