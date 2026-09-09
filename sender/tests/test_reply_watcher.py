import email
import sys
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

import db as db_module
import pytest
from reply_watcher import (
    classify_message, extract_ooo_return_date, get_text_body,
    poll_and_process, fetch_recent_uids, classify_bounce_severity,
)


@pytest.fixture
def conn(tmp_path):
    c = db_module.get_connection(str(tmp_path / "test.db"))
    db_module.init_db(c)
    yield c
    c.close()


# --- classify_message --------------------------------------------------------------

def test_classify_bounce_by_sender():
    assert classify_message("mailer-daemon@google.com", "Delivery failure", "") == "bounce"


def test_classify_bounce_by_subject():
    assert classify_message("random@x.com", "Undeliverable: Mail", "") == "bounce"


def test_classify_unsubscribe():
    assert classify_message("jane@acme.com", "Re: quick question", "Please unsubscribe me from this list.") == "unsubscribe"


def test_classify_ooo():
    assert classify_message("jane@acme.com", "Automatic reply: Out of Office", "I am out of the office until Monday.") == "ooo"


def test_classify_genuine_reply():
    assert classify_message("jane@acme.com", "Re: quick question", "Sure, let's talk. When works?") == "reply"


def test_classify_bounce_takes_priority_over_ooo_keywords_if_both_present():
    # edge case: a bounce notification quoting an OOO reply in its body
    assert classify_message("mailer-daemon@x.com", "Undelivered Mail", "out of office") == "bounce"


# --- classify_bounce_severity -------------------------------------------------------

def test_bounce_severity_dsn_5xx_is_hard():
    assert classify_bounce_severity("Status: 5.1.1\nRecipient address rejected") == "hard"


def test_bounce_severity_dsn_4xx_is_soft():
    assert classify_bounce_severity("Status: 4.2.2\nMailbox full") == "soft"


def test_bounce_severity_keyword_hard_no_dsn():
    assert classify_bounce_severity("550 no such user here") == "hard"


def test_bounce_severity_keyword_soft_no_dsn():
    assert classify_bounce_severity("452 mailbox full, try again later") == "soft"


def test_bounce_severity_defaults_to_hard_when_ambiguous():
    assert classify_bounce_severity("Your message could not be delivered.") == "hard"


def test_bounce_severity_dsn_code_wins_over_conflicting_keywords():
    # DSN status is machine-generated and more reliable than keyword text
    assert classify_bounce_severity("Status: 4.4.1\nno such user (transient DNS issue)") == "soft"


# --- extract_ooo_return_date -------------------------------------------------------

def test_extract_ooo_return_date_slash_format():
    result = extract_ooo_return_date("I will be back on 12/25/2026 and will respond then.")
    assert result is not None
    assert result.month == 12 and result.day == 25


def test_extract_ooo_return_date_month_name_format():
    result = extract_ooo_return_date("Returning Dec 25, see you then.")
    assert result is not None
    assert result.month == 12 and result.day == 25


def test_extract_ooo_return_date_none_when_unparseable():
    assert extract_ooo_return_date("I am away indefinitely.") is None


def test_extract_ooo_return_date_none_for_empty_body():
    assert extract_ooo_return_date("") is None
    assert extract_ooo_return_date(None) is None


# --- get_text_body -----------------------------------------------------------------

def test_get_text_body_simple_message():
    msg = EmailMessage()
    msg.set_content("hello world")
    parsed = email.message_from_bytes(msg.as_bytes())
    assert "hello world" in get_text_body(parsed)


def test_get_text_body_multipart_prefers_plain_text():
    msg = EmailMessage()
    msg.set_content("plain text version")
    msg.add_alternative("<p>html version</p>", subtype="html")
    parsed = email.message_from_bytes(msg.as_bytes())
    assert "plain text version" in get_text_body(parsed)


# --- poll_and_process (mocked IMAP) -------------------------------------------------

def _make_raw_email(from_addr: str, subject: str, body: str) -> bytes:
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = "me@mine.com"
    msg["Subject"] = subject
    msg.set_content(body)
    return msg.as_bytes()


def _mock_imap_with_messages(messages: dict):
    """messages: {uid_bytes: raw_email_bytes}"""
    mock_imap = MagicMock()
    uids = list(messages.keys())
    mock_imap.uid.side_effect = lambda cmd, *args: (
        ("OK", [b" ".join(uids)]) if cmd == "search"
        else ("OK", [(b"1 (BODY[])", messages[args[0]])]) if cmd == "fetch"
        else ("NO", [])
    )
    return mock_imap


def test_poll_and_process_marks_unsubscribe(conn):
    db_module.upsert_lead(conn, "jane@acme.com")
    raw = _make_raw_email("jane@acme.com", "Re: quick q", "Please unsubscribe me, not interested.")
    imap = _mock_imap_with_messages({b"101": raw})

    summary = poll_and_process(conn, imap, log=lambda *a: None)

    assert summary["unsubscribe"] == 1
    assert db_module.get_lead(conn, "jane@acme.com")["status"] == "unsubscribed"
    assert db_module.is_suppressed(conn, "jane@acme.com") is True


def test_poll_and_process_marks_hard_bounce(conn):
    db_module.upsert_lead(conn, "jane@acme.com")
    raw = _make_raw_email("mailer-daemon@mail.acme.com", "Undelivered Mail Returned to Sender",
                           "The following message could not be delivered to jane@acme.com: "
                           "550 5.1.1 user unknown")
    imap = _mock_imap_with_messages({b"102": raw})

    summary = poll_and_process(conn, imap, log=lambda *a: None)

    assert summary["bounce_hard"] == 1
    assert db_module.get_lead(conn, "jane@acme.com")["status"] == "bounced"
    assert db_module.is_suppressed(conn, "jane@acme.com") is True


def test_poll_and_process_soft_bounce_does_not_suppress(conn):
    # regression: this used to permanently suppress on ANY bounce-shaped
    # message, including transient ones -- a soft bounce (mailbox full)
    # isn't "this address doesn't exist" and must not kill a good lead
    db_module.upsert_lead(conn, "jane@acme.com")
    raw = _make_raw_email("mailer-daemon@mail.acme.com", "Delivery Status Notification (Delay)",
                           "Delivery to jane@acme.com has been delayed: "
                           "450 4.2.2 mailbox full, try again later")
    imap = _mock_imap_with_messages({b"106": raw})

    summary = poll_and_process(conn, imap, log=lambda *a: None)

    assert summary["bounce_soft"] == 1
    lead = db_module.get_lead(conn, "jane@acme.com")
    assert lead["status"] == "active"  # NOT bounced
    assert db_module.is_suppressed(conn, "jane@acme.com") is False
    assert lead["next_send_at"] is not None  # rescheduled for retry


def test_poll_and_process_marks_genuine_reply(conn):
    db_module.upsert_lead(conn, "jane@acme.com")
    raw = _make_raw_email("jane@acme.com", "Re: quick question", "Interested, tell me more.")
    imap = _mock_imap_with_messages({b"103": raw})

    summary = poll_and_process(conn, imap, log=lambda *a: None)

    assert summary["reply"] == 1
    assert db_module.get_lead(conn, "jane@acme.com")["status"] == "replied"
    # a plain reply must NOT suppress the domain
    assert db_module.is_suppressed(conn, "john@acme.com") is False


def test_poll_and_process_saves_reply_text_for_feedback_loop(conn):
    # regression: reply_watcher previously flipped status but discarded the
    # actual reply content -- without the text (and which step/subject it
    # answered) saved, Stage 10's feedback loop has nothing to learn from.
    db_module.upsert_lead(conn, "jane@acme.com")
    conn.execute("UPDATE leads SET sequence_step = 2 WHERE email = 'jane@acme.com'")
    conn.commit()

    raw = _make_raw_email("jane@acme.com", "Re: quick question", "Interested, tell me more.")
    imap = _mock_imap_with_messages({b"103": raw})

    poll_and_process(conn, imap, log=lambda *a: None)

    saved = db_module.get_unclassified_replies(conn)
    assert len(saved) == 1
    assert saved[0]["email"] == "jane@acme.com"
    assert saved[0]["body_text"] == "Interested, tell me more."
    assert saved[0]["step"] == 2  # the step the lead was on when they replied
    assert saved[0]["subject"] == "Re: quick question"


def test_poll_and_process_skips_already_processed_uid(conn):
    db_module.upsert_lead(conn, "jane@acme.com")
    db_module.mark_reply_processed(conn, "104", "reply")
    raw = _make_raw_email("jane@acme.com", "Re: quick question", "hello")
    imap = _mock_imap_with_messages({b"104": raw})

    summary = poll_and_process(conn, imap, log=lambda *a: None)

    assert summary["reply"] == 0  # already processed, skipped entirely


def test_poll_and_process_skips_unknown_sender(conn):
    raw = _make_raw_email("stranger@somewhere.com", "hi", "random message not from a lead")
    imap = _mock_imap_with_messages({b"105": raw})

    summary = poll_and_process(conn, imap, log=lambda *a: None)

    assert summary["skipped_unknown_lead"] == 1
