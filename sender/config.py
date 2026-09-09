"""
Central config for the sending engine. All values come from environment
variables (or a local .env file, loaded automatically if python-dotenv is
installed) -- never hardcode credentials in code.

Copy .env.example to .env and fill in real values before running sender.py
or reply_watcher.py for real. Nothing here sends anything by itself.
"""
import os
from datetime import date, datetime

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # fine without it -- just means you set real env vars yourself


def _get(name: str, default=None, required: bool = False):
    val = os.environ.get(name, default)
    if required and not val:
        raise SystemExit(f"Missing required environment variable: {name} (see .env.example)")
    return val


def _get_int(name: str, default: int) -> int:
    val = os.environ.get(name)
    return int(val) if val else default


def _get_date(name: str, default: date) -> date:
    val = os.environ.get(name)
    return datetime.strptime(val, "%Y-%m-%d").date() if val else default


class Config:
    # SMTP (sending)
    SMTP_HOST = _get("SMTP_HOST", "smtp.gmail.com")
    SMTP_PORT = _get_int("SMTP_PORT", 587)
    SMTP_USER = _get("SMTP_USER")  # required at send time, not at import time
    SMTP_PASSWORD = _get("SMTP_PASSWORD")  # app password, never your real login password

    # IMAP (reply/bounce polling)
    IMAP_HOST = _get("IMAP_HOST", "imap.gmail.com")
    IMAP_PORT = _get_int("IMAP_PORT", 993)
    IMAP_USER = _get("IMAP_USER") or SMTP_USER
    IMAP_PASSWORD = _get("IMAP_PASSWORD") or SMTP_PASSWORD

    # Identity + required compliance footer (CAN-SPAM: physical address + opt-out)
    FROM_NAME = _get("FROM_NAME", "")
    REPLY_TO = _get("REPLY_TO") or SMTP_USER
    PHYSICAL_ADDRESS = _get("PHYSICAL_ADDRESS", "")  # REQUIRED by law for commercial email in the US
    UNSUBSCRIBE_TEXT = _get("UNSUBSCRIBE_TEXT", "Reply STOP to opt out of future emails.")
    # List-Unsubscribe header (RFC 2369/8058) -- Gmail/Yahoo have required this
    # since Feb 2024 for bulk senders, and it's a real deliverability signal
    # regardless of volume. Defaults to the same reply address the footer text
    # already points at, so it works with zero extra config; reply_watcher.py
    # already classifies "unsubscribe" text arriving there. UNSUBSCRIBE_URL is
    # optional -- only set it if you're hosting a one-click unsubscribe page
    # somewhere; without one, List-Unsubscribe-Post (true one-click) isn't sent,
    # since it has no HTTP endpoint to be meaningful against.
    UNSUBSCRIBE_MAILTO = _get("UNSUBSCRIBE_MAILTO") or REPLY_TO
    UNSUBSCRIBE_URL = _get("UNSUBSCRIBE_URL")

    # Warmup / volume control
    MAILBOX_NAME = _get("MAILBOX_NAME") or SMTP_USER
    WARMUP_START_DATE = _get_date("WARMUP_START_DATE", date.today())
    DAILY_CAP_OVERRIDE = os.environ.get("DAILY_CAP_OVERRIDE")
    DAILY_CAP_OVERRIDE = int(DAILY_CAP_OVERRIDE) if DAILY_CAP_OVERRIDE else None

    # Pacing between individual sends (seconds) -- randomized between these
    MIN_SEND_DELAY_SEC = _get_int("MIN_SEND_DELAY_SEC", 60)
    MAX_SEND_DELAY_SEC = _get_int("MAX_SEND_DELAY_SEC", 180)

    # Storage
    DB_PATH = _get("SENDER_DB_PATH", "sender_state.db")

    @classmethod
    def require_compliance_ready(cls):
        """The one thing required regardless of single- or multi-mailbox
        mode: a real physical address in the footer (CAN-SPAM). Per-mailbox
        SMTP credentials are validated separately -- mailboxes.load_mailboxes
        already requires smtp_user/smtp_password on every entry, and Config's
        SMTP_USER/PASSWORD are irrelevant once a mailbox pool is in play."""
        if not cls.PHYSICAL_ADDRESS:
            raise SystemExit("PHYSICAL_ADDRESS is required (CAN-SPAM) even in multi-mailbox mode. See .env.example")

    @classmethod
    def require_send_ready(cls):
        """Call this before any real send in SINGLE-mailbox mode -- fails
        loudly if creds/compliance fields are missing, instead of silently
        sending a non-compliant email or crashing mid-batch on the 400th lead."""
        missing = []
        if not cls.SMTP_USER:
            missing.append("SMTP_USER")
        if not cls.SMTP_PASSWORD:
            missing.append("SMTP_PASSWORD")
        if not cls.PHYSICAL_ADDRESS:
            missing.append("PHYSICAL_ADDRESS (required by CAN-SPAM for commercial email)")
        if missing:
            raise SystemExit(f"Not ready to send -- missing: {', '.join(missing)}. See .env.example")
