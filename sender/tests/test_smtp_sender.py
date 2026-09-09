import smtplib
import sys
from email import message_from_bytes
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from smtp_sender import build_message, send_email


# --- build_message ---------------------------------------------------------------

def test_build_message_includes_compliance_footer():
    msg, mid = build_message(
        "Jane", "jane@acme.com", "lead@target.com", "quick question", "Hi there, worth a look?",
        "jane@acme.com", "123 Main St, City, ST", "Reply STOP to opt out.",
    )
    body = msg.get_content()
    assert "123 Main St" in body
    assert "Reply STOP to opt out." in body
    assert "worth a look?" in body


def test_build_message_sets_headers():
    msg, mid = build_message(
        "Jane", "jane@acme.com", "lead@target.com", "quick question", "body text",
        "jane@acme.com", "addr", "unsub",
    )
    assert msg["To"] == "lead@target.com"
    assert "jane@acme.com" in msg["From"]
    assert msg["Reply-To"] == "jane@acme.com"
    assert msg["Message-ID"] == mid


def test_build_message_threads_followup_as_reply():
    msg, mid = build_message(
        "Jane", "jane@acme.com", "lead@target.com", "quick question", "follow up body",
        "jane@acme.com", "addr", "unsub",
        in_reply_to="<original@acme.com>", references="<original@acme.com>",
    )
    assert msg["In-Reply-To"] == "<original@acme.com>"
    assert msg["References"] == "<original@acme.com>"
    assert msg["Subject"].startswith("Re:")


def test_build_message_does_not_double_prefix_re():
    msg, mid = build_message(
        "Jane", "jane@acme.com", "lead@target.com", "Re: quick question", "body",
        "jane@acme.com", "addr", "unsub", in_reply_to="<x@acme.com>",
    )
    assert msg["Subject"] == "Re: quick question"


# --- List-Unsubscribe (RFC 8058/2369) -- Gmail/Yahoo bulk-sender requirement ------

def test_build_message_no_list_unsubscribe_header_when_neither_given():
    msg, mid = build_message(
        "Jane", "jane@acme.com", "lead@target.com", "subj", "body",
        "jane@acme.com", "addr", "unsub",
    )
    assert "List-Unsubscribe" not in msg


def test_build_message_includes_list_unsubscribe_mailto():
    msg, mid = build_message(
        "Jane", "jane@acme.com", "lead@target.com", "subj", "body",
        "jane@acme.com", "addr", "unsub",
        unsubscribe_mailto="unsub@acme.com",
    )
    header = msg["List-Unsubscribe"]
    assert "<mailto:unsub@acme.com?subject=unsubscribe>" in header


def test_build_message_mailto_only_omits_one_click_post():
    # List-Unsubscribe-Post (RFC 8058 one-click) is only valid with an HTTP
    # URL target -- a mailto-only header must not claim one-click support
    msg, mid = build_message(
        "Jane", "jane@acme.com", "lead@target.com", "subj", "body",
        "jane@acme.com", "addr", "unsub",
        unsubscribe_mailto="unsub@acme.com",
    )
    assert "List-Unsubscribe-Post" not in msg


def test_build_message_includes_list_unsubscribe_url_and_one_click_post():
    msg, mid = build_message(
        "Jane", "jane@acme.com", "lead@target.com", "subj", "body",
        "jane@acme.com", "addr", "unsub",
        unsubscribe_url="https://acme.com/unsub?id=123",
    )
    assert "<https://acme.com/unsub?id=123>" in msg["List-Unsubscribe"]
    assert msg["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"


def test_build_message_includes_both_mailto_and_url_when_given():
    msg, mid = build_message(
        "Jane", "jane@acme.com", "lead@target.com", "subj", "body",
        "jane@acme.com", "addr", "unsub",
        unsubscribe_mailto="unsub@acme.com", unsubscribe_url="https://acme.com/unsub?id=123",
    )
    header = msg["List-Unsubscribe"]
    assert "mailto:unsub@acme.com" in header
    assert "https://acme.com/unsub?id=123" in header


def test_build_message_generates_unique_message_ids():
    _, mid1 = build_message("J", "a@x.com", "b@y.com", "s", "b", "a@x.com", "addr", "u")
    _, mid2 = build_message("J", "a@x.com", "b@y.com", "s", "b", "a@x.com", "addr", "u")
    assert mid1 != mid2


# --- send_email: mocked failure paths (never crash the batch) ----------------------

@patch("smtp_sender.smtplib.SMTP")
def test_send_email_auth_failure_returns_false_not_raise(mock_smtp_cls):
    instance = MagicMock()
    instance.__enter__.return_value = instance
    instance.login.side_effect = smtplib.SMTPAuthenticationError(535, b"bad creds")
    mock_smtp_cls.return_value = instance

    ok, result = send_email(
        "smtp.x.com", 587, "user", "pass", "Jane", "lead@target.com",
        "subj", "body", "jane@x.com", "addr", "unsub",
    )
    assert ok is False
    assert "auth failed" in result


@patch("smtp_sender.smtplib.SMTP")
def test_send_email_recipient_refused_returns_false(mock_smtp_cls):
    instance = MagicMock()
    instance.__enter__.return_value = instance
    instance.send_message.side_effect = smtplib.SMTPRecipientsRefused({"lead@target.com": (550, b"no such user")})
    mock_smtp_cls.return_value = instance

    ok, result = send_email(
        "smtp.x.com", 587, "user", "pass", "Jane", "lead@target.com",
        "subj", "body", "jane@x.com", "addr", "unsub",
    )
    assert ok is False
    assert "recipient refused" in result


@patch("smtp_sender.smtplib.SMTP")
def test_send_email_generic_exception_returns_false_not_raise(mock_smtp_cls):
    mock_smtp_cls.side_effect = ConnectionRefusedError("network down")
    ok, result = send_email(
        "smtp.x.com", 587, "user", "pass", "Jane", "lead@target.com",
        "subj", "body", "jane@x.com", "addr", "unsub",
    )
    assert ok is False
    assert "send failed" in result


@patch("smtp_sender.smtplib.SMTP")
def test_send_email_success_returns_message_id(mock_smtp_cls):
    instance = MagicMock()
    instance.__enter__.return_value = instance
    mock_smtp_cls.return_value = instance

    ok, result = send_email(
        "smtp.x.com", 587, "user", "pass", "Jane", "lead@target.com",
        "subj", "body", "jane@x.com", "addr", "unsub",
    )
    assert ok is True
    assert result.startswith("<") and result.endswith(">")
    instance.starttls.assert_called_once()
    instance.login.assert_called_once_with("user", "pass")
    instance.send_message.assert_called_once()


# --- send_email: REAL local SMTP server, no mocks, zero external network -----------
# Verifies the actual protocol code path (connect, send, MIME structure) works,
# not just that our own mocked stand-ins were called correctly.

aiosmtpd = pytest.importorskip("aiosmtpd.controller")


class _CapturingHandler:
    def __init__(self):
        self.received = []

    async def handle_DATA(self, server, session, envelope):
        self.received.append(envelope)
        return "250 Message accepted for delivery"


def _find_free_port() -> int:
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def local_smtp_server():
    from aiosmtpd.controller import Controller
    handler = _CapturingHandler()
    # aiosmtpd's own internal readiness-check connects using self.port right
    # after bind -- on Windows, port=0 (ask-OS-for-a-port) fails that check
    # ("address not valid in its context") because of how it reads the bound
    # port back. Picking a free port ourselves and passing it explicitly
    # sidesteps that platform quirk.
    port = _find_free_port()
    controller = Controller(handler, hostname="127.0.0.1", port=port)
    controller.start()
    yield handler, controller.hostname, controller.port
    controller.stop()


def test_send_email_real_local_smtp_roundtrip(local_smtp_server):
    handler, host, port = local_smtp_server
    ok, message_id = send_email(
        smtp_host=host, smtp_port=port, smtp_user="jane@acme.com", smtp_password=None,
        from_name="Jane Diaz", to_addr="lead@target.com",
        subject="quick question", body="Saw your team page, worth a look?",
        reply_to="jane@acme.com", physical_address="123 Main St, City, ST",
        unsubscribe_text="Reply STOP to opt out.",
        use_starttls=False,  # local test server has no TLS cert -- not testing TLS here
    )
    assert ok is True
    assert len(handler.received) == 1

    envelope = handler.received[0]
    assert envelope.mail_from == "jane@acme.com" or "acme.com" in envelope.mail_from
    assert envelope.rcpt_tos == ["lead@target.com"]

    received_msg = message_from_bytes(envelope.content)
    assert received_msg["Subject"] == "quick question"
    assert received_msg["Reply-To"] == "jane@acme.com"
    body = received_msg.get_payload(decode=True).decode("utf-8")
    assert "Saw your team page" in body
    assert "123 Main St" in body
    assert "Reply STOP" in body


def test_send_email_real_local_smtp_followup_threads_correctly(local_smtp_server):
    handler, host, port = local_smtp_server
    ok, message_id = send_email(
        smtp_host=host, smtp_port=port, smtp_user="jane@acme.com", smtp_password=None,
        from_name="Jane Diaz", to_addr="lead@target.com",
        subject="quick question", body="Bumping this.",
        reply_to="jane@acme.com", physical_address="123 Main St",
        unsubscribe_text="Reply STOP.",
        in_reply_to="<original-msg-id@acme.com>", references="<original-msg-id@acme.com>",
        use_starttls=False,
    )
    assert ok is True
    received_msg = message_from_bytes(handler.received[0].content)
    assert received_msg["In-Reply-To"] == "<original-msg-id@acme.com>"
    assert received_msg["Subject"] == "Re: quick question"
