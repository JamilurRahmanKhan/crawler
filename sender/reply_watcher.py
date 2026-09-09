"""
Polls the sending mailbox via IMAP for replies/bounces, classifies each one,
and updates lead state accordingly. This is the free replacement for
Smartlead's auto-reply/bounce detection.

Uses BODY.PEEK (not a plain FETCH) so polling never marks the account's real
unread messages as read -- matters if this is also a mailbox a person reads.
Dedup is tracked in our own DB (processed_replies), independent of the
mailbox's \\Seen flag, so a re-run never double-processes the same message.

Classification is intentionally simple and rule-based (no LLM call here) --
bounce/unsubscribe/OOO detection via sender/subject/body patterns is reliable
enough for those three categories. A genuine human reply is never
auto-answered; it's flagged for a person to read and respond to. If you want
finer-grained interested/not-interested/wrong-person classification on top of
"this is a genuine reply", that's a good use for an LLM call layered on top
of what this already sorts out -- not rebuilt here.
"""
import email
import imaplib
import re
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr

import db as db_module

BOUNCE_SENDER_RE = re.compile(r"mailer-daemon|postmaster|mail delivery|no-?reply@.*delivery", re.I)
BOUNCE_SUBJECT_RE = re.compile(
    r"undeliver|delivery status notification|delivery failure|failure notice|"
    r"returned mail|mail delivery failed|couldn.t be delivered",
    re.I,
)
UNSUBSCRIBE_RE = re.compile(
    r"\bunsubscribe\b|stop emailing|remove me from|take me off|stop contacting|"
    r"do not (email|contact) me again|no longer interested.{0,20}remove",
    re.I,
)
OOO_RE = re.compile(
    r"out of (the )?office|automatic reply|auto-reply|on vacation|"
    r"currently (away|out|on leave)|annual leave|will be back",
    re.I,
)
# best-effort "back on <date>" / "return<ing>? <date>" extraction -- if this
# fails to find anything, caller falls back to a fixed default delay
OOO_DATE_RE = re.compile(
    r"(?:back|return(?:ing)?|available)\s+(?:on\s+)?"
    r"(\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?|"
    r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2})",
    re.I,
)
DEFAULT_OOO_DELAY_DAYS = 7

# DSN (Delivery Status Notification) status codes: RFC 3463. First digit
# 5 = permanent failure (address doesn't exist, domain rejects, etc.) --
# suppress for good. First digit 4 = temporary failure (mailbox full,
# greylisting, server briefly down) -- NOT the same thing as "this address
# is dead." Treating every bounce as permanent was a real bug: a soft bounce
# would permanently kill a perfectly good lead over a transient hiccup.
DSN_STATUS_RE = re.compile(r"\bStatus:\s*([45])\.\d{1,3}\.\d{1,3}", re.I)
HARD_BOUNCE_KEYWORDS_RE = re.compile(
    r"no such user|does not exist|user unknown|invalid recipient|"
    r"recipient rejected|mailbox not found|address rejected|"
    r"no mailbox here|unknown user|unrouteable address",
    re.I,
)
SOFT_BOUNCE_KEYWORDS_RE = re.compile(
    r"mailbox full|quota exceeded|over quota|try again later|"
    r"temporarily deferred|greylist|message delayed|"
    r"temporary failure|service (?:not|un)available|throttl",
    re.I,
)
SOFT_BOUNCE_RETRY_DAYS = 2


def classify_bounce_severity(full_text: str) -> str:
    """Returns 'hard' | 'soft'. Checks the machine-readable DSN Status: code
    first (most reliable when present), falls back to keyword heuristics,
    and defaults to 'hard' when genuinely ambiguous -- safer to suppress an
    unclear bounce than to keep hammering a broken mailbox and risk sender
    reputation, but this is a documented judgment call, not a certainty."""
    dsn_match = DSN_STATUS_RE.search(full_text or "")
    if dsn_match:
        return "hard" if dsn_match.group(1) == "5" else "soft"
    if SOFT_BOUNCE_KEYWORDS_RE.search(full_text or "") and not HARD_BOUNCE_KEYWORDS_RE.search(full_text or ""):
        return "soft"
    return "hard"


def classify_message(from_addr: str, subject: str, body: str) -> str:
    """Returns 'bounce' | 'unsubscribe' | 'ooo' | 'reply'."""
    if BOUNCE_SENDER_RE.search(from_addr) or BOUNCE_SUBJECT_RE.search(subject or ""):
        return "bounce"
    combined = f"{subject or ''}\n{body or ''}"
    if UNSUBSCRIBE_RE.search(combined):
        return "unsubscribe"
    if OOO_RE.search(combined):
        return "ooo"
    return "reply"


def extract_ooo_return_date(body: str) -> datetime:
    """Best-effort. Returns a datetime to resume sending, or None if no date
    could be parsed (caller should fall back to a fixed default delay)."""
    match = OOO_DATE_RE.search(body or "")
    if not match:
        return None
    raw = match.group(1)
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%m/%d", "%b %d", "%B %d"):
        try:
            parsed = datetime.strptime(raw, fmt)
            if parsed.year == 1900:  # formats without a year default to 1900
                parsed = parsed.replace(year=datetime.now().year)
            return parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def get_text_body(msg: email.message.Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and not part.get_filename():
                try:
                    return part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", errors="replace")
                except Exception:
                    continue
        return ""
    try:
        return msg.get_payload(decode=True).decode(msg.get_content_charset() or "utf-8", errors="replace")
    except Exception:
        return ""


def get_full_raw_text(msg: email.message.Message) -> str:
    """Concatenates every part's payload -- a DSN's machine-readable
    Status: code usually lives in a message/delivery-status part, separate
    from the human-readable text/plain part get_text_body() returns."""
    chunks = []
    if msg.is_multipart():
        for part in msg.walk():
            try:
                payload = part.get_payload(decode=True)
                if payload:
                    chunks.append(payload.decode(part.get_content_charset() or "utf-8", errors="replace"))
            except Exception:
                continue
    else:
        try:
            payload = msg.get_payload(decode=True)
            if payload:
                chunks.append(payload.decode(msg.get_content_charset() or "utf-8", errors="replace"))
        except Exception:
            pass
    return "\n".join(chunks)


def connect_imap(host: str, port: int, user: str, password: str) -> imaplib.IMAP4_SSL:
    conn = imaplib.IMAP4_SSL(host, port)
    conn.login(user, password)
    conn.select("INBOX")
    return conn


def fetch_recent_uids(imap_conn: imaplib.IMAP4_SSL, since_days: int = 10) -> list:
    since_date = (datetime.now() - timedelta(days=since_days)).strftime("%d-%b-%Y")
    status, data = imap_conn.uid("search", None, f'(SINCE {since_date})')
    if status != "OK" or not data or not data[0]:
        return []
    return data[0].split()


def fetch_message_by_uid(imap_conn: imaplib.IMAP4_SSL, uid: bytes) -> email.message.Message:
    # BODY.PEEK[] -- fetches without setting \Seen, so this never marks the
    # user's real mailbox messages as read out from under them
    status, data = imap_conn.uid("fetch", uid, "(BODY.PEEK[])")
    if status != "OK" or not data or not data[0]:
        return None
    raw = data[0][1]
    return email.message_from_bytes(raw)


def poll_and_process(conn, imap_conn, log=print) -> dict:
    """One polling pass. Returns counts by classification. `conn` is the
    sqlite3 connection from db.py; `imap_conn` is an already-connected,
    already-logged-in IMAP4_SSL instance (see connect_imap)."""
    summary = {"bounce_hard": 0, "bounce_soft": 0, "unsubscribe": 0, "ooo": 0, "reply": 0,
               "skipped_unknown_lead": 0, "errors": 0}
    uids = fetch_recent_uids(imap_conn)

    for uid in uids:
        uid_str = uid.decode() if isinstance(uid, bytes) else str(uid)
        if db_module.is_reply_processed(conn, uid_str):
            continue
        try:
            msg = fetch_message_by_uid(imap_conn, uid)
            if msg is None:
                continue
            from_name, from_addr = parseaddr(msg.get("From", ""))
            from_addr = from_addr.lower().strip()
            subject = msg.get("Subject", "")
            body = get_text_body(msg)

            classification = classify_message(from_addr, subject, body)

            lead = db_module.get_lead(conn, from_addr) if from_addr else None
            if lead is None:
                # not a message from/about a known lead -- e.g. a bounce whose
                # From is mailer-daemon but the ORIGINAL recipient is buried in
                # the body. Best-effort: try to find a known lead email inside
                # the bounce body itself before giving up.
                lead = _find_lead_mentioned_in_body(conn, body)

            if lead is None:
                summary["skipped_unknown_lead"] += 1
                db_module.mark_reply_processed(conn, uid_str, f"{classification}:unmatched")
                continue

            lead_email = lead["email"]
            if classification == "bounce":
                severity = classify_bounce_severity(get_full_raw_text(msg))
                if severity == "hard":
                    db_module.mark_bounced(conn, lead_email)
                    log(f"[reply_watcher] {lead_email}: hard bounce -> suppressed permanently")
                    classification = "bounce_hard"
                else:
                    retry_at = datetime.now(timezone.utc) + timedelta(days=SOFT_BOUNCE_RETRY_DAYS)
                    db_module.reschedule(conn, lead_email, retry_at.isoformat())
                    log(f"[reply_watcher] {lead_email}: soft bounce -> retrying {retry_at.date()}, NOT suppressed")
                    classification = "bounce_soft"
            elif classification == "unsubscribe":
                db_module.mark_unsubscribed(conn, lead_email, domain_wide=True)
                log(f"[reply_watcher] {lead_email}: unsubscribe -> suppressed (domain-wide)")
            elif classification == "ooo":
                resume_at = extract_ooo_return_date(body)
                if resume_at is None:
                    resume_at = datetime.now(timezone.utc) + timedelta(days=DEFAULT_OOO_DELAY_DAYS)
                db_module.reschedule(conn, lead_email, resume_at.isoformat())
                log(f"[reply_watcher] {lead_email}: out-of-office -> rescheduled to {resume_at.date()}")
            else:  # genuine reply
                db_module.save_reply(conn, lead_email, lead["sequence_step"], subject, body)
                db_module.mark_replied(conn, lead_email)
                log(f"[reply_watcher] {lead_email}: REPLY -- sequence stopped, human review needed")

            summary[classification] += 1
            db_module.mark_reply_processed(conn, uid_str, classification)

        except Exception as e:
            summary["errors"] += 1
            log(f"[reply_watcher] error processing uid {uid_str}: {e}")
            continue

    return summary


def _find_lead_mentioned_in_body(conn, body: str):
    """For bounces where the From is mailer-daemon and the failed recipient
    address is only mentioned in the bounce body -- best-effort scan for any
    known lead email appearing in the text."""
    if not body:
        return None
    candidates = re.findall(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", body)
    for addr in candidates:
        lead = db_module.get_lead(conn, addr)
        if lead is not None:
            return lead
    return None
