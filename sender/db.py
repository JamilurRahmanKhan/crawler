"""
SQLite-backed state for the sending engine. Tracks per-lead sequence
progress, mailbox daily send counts (for warmup cap enforcement), and the
permanent suppression list -- the single most important table here, since
sending to someone twice after they said no is the #1 way to burn a lead
and a domain's reputation at once.

No server needed -- one file, safe for a single-process sender (this is not
designed for multiple sender.py processes writing concurrently; run one at
a time per mailbox, which is the realistic setup at this scale anyway).
"""
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_connection(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS leads (
        email TEXT PRIMARY KEY,
        domain TEXT,
        name TEXT,
        sequence_step INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'active',
        last_sent_at TEXT,
        next_send_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS suppression (
        value TEXT NOT NULL,
        scope TEXT NOT NULL CHECK(scope IN ('email', 'domain')),
        reason TEXT,
        suppressed_at TEXT NOT NULL,
        PRIMARY KEY (value, scope)
    );

    CREATE TABLE IF NOT EXISTS send_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT NOT NULL,
        step INTEGER NOT NULL,
        subject TEXT,
        message_id TEXT,
        mailbox TEXT,
        sent_at TEXT NOT NULL,
        status TEXT NOT NULL,
        error TEXT
    );

    CREATE TABLE IF NOT EXISTS mailbox_daily_counts (
        mailbox TEXT NOT NULL,
        day TEXT NOT NULL,
        count INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (mailbox, day)
    );

    CREATE TABLE IF NOT EXISTS replies (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT NOT NULL,
        step INTEGER,
        subject TEXT,
        body_text TEXT,
        received_at TEXT NOT NULL,
        sentiment TEXT,
        sentiment_confidence INTEGER,
        classified_at TEXT
    );

    CREATE TABLE IF NOT EXISTS processed_replies (
        uid TEXT PRIMARY KEY,
        processed_at TEXT NOT NULL,
        classification TEXT
    );
    """)
    conn.commit()

    # Safe migration for a leads table created before meeting_booked_at
    # existed -- SQLite has no "ADD COLUMN IF NOT EXISTS", so add and
    # swallow the "duplicate column" error if it's already there. This
    # must not break an existing production DB on upgrade.
    try:
        conn.execute("ALTER TABLE leads ADD COLUMN meeting_booked_at TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass


def is_reply_processed(conn: sqlite3.Connection, uid: str) -> bool:
    return conn.execute("SELECT 1 FROM processed_replies WHERE uid = ?", (uid,)).fetchone() is not None


def mark_reply_processed(conn: sqlite3.Connection, uid: str, classification: str):
    conn.execute(
        "INSERT OR REPLACE INTO processed_replies (uid, processed_at, classification) VALUES (?, ?, ?)",
        (uid, now_iso(), classification),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Suppression -- checked before every single send, no exceptions
# ---------------------------------------------------------------------------

def add_suppression(conn: sqlite3.Connection, value: str, scope: str, reason: str):
    value = value.strip().lower()
    conn.execute(
        "INSERT OR REPLACE INTO suppression (value, scope, reason, suppressed_at) VALUES (?, ?, ?, ?)",
        (value, scope, reason, now_iso()),
    )
    conn.commit()


def is_suppressed(conn: sqlite3.Connection, email: str) -> bool:
    email = email.strip().lower()
    domain = email.rsplit("@", 1)[-1] if "@" in email else ""
    row = conn.execute(
        "SELECT 1 FROM suppression WHERE (value = ? AND scope = 'email') "
        "OR (value = ? AND scope = 'domain') LIMIT 1",
        (email, domain),
    ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# Leads / sequence state
# ---------------------------------------------------------------------------

def upsert_lead(conn: sqlite3.Connection, email: str, domain: str = None, name: str = None):
    email = email.strip().lower()
    existing = conn.execute("SELECT email FROM leads WHERE email = ?", (email,)).fetchone()
    if existing:
        return
    conn.execute(
        "INSERT INTO leads (email, domain, name, sequence_step, status, created_at, updated_at) "
        "VALUES (?, ?, ?, 0, 'active', ?, ?)",
        (email, domain or email.rsplit("@", 1)[-1], name, now_iso(), now_iso()),
    )
    conn.commit()


def get_lead(conn: sqlite3.Connection, email: str) -> sqlite3.Row:
    return conn.execute("SELECT * FROM leads WHERE email = ?", (email.strip().lower(),)).fetchone()


def get_leads_due_for_send(conn: sqlite3.Connection, today: str = None) -> list:
    """Active leads whose next_send_at has arrived (or was never set --
    meaning ready for their first send). Suppressed leads are excluded here
    too, as a second check beyond whatever called this."""
    today = today or now_iso()
    rows = conn.execute(
        "SELECT * FROM leads WHERE status = 'active' "
        "AND (next_send_at IS NULL OR next_send_at <= ?) "
        "ORDER BY created_at ASC",
        (today,),
    ).fetchall()
    return [r for r in rows if not is_suppressed(conn, r["email"])]


def mark_sent(conn: sqlite3.Connection, email: str, step: int, subject: str,
              message_id: str, mailbox: str, next_send_at: str = None):
    email = email.strip().lower()
    conn.execute(
        "INSERT INTO send_log (email, step, subject, message_id, mailbox, sent_at, status) "
        "VALUES (?, ?, ?, ?, ?, ?, 'sent')",
        (email, step, subject, message_id, mailbox, now_iso()),
    )
    conn.execute(
        "UPDATE leads SET sequence_step = ?, last_sent_at = ?, next_send_at = ?, updated_at = ? WHERE email = ?",
        (step, now_iso(), next_send_at, now_iso(), email),
    )
    conn.commit()


def mark_send_failed(conn: sqlite3.Connection, email: str, step: int, error: str, mailbox: str):
    conn.execute(
        "INSERT INTO send_log (email, step, mailbox, sent_at, status, error) VALUES (?, ?, ?, ?, 'failed', ?)",
        (email.strip().lower(), step, mailbox, now_iso(), error),
    )
    conn.commit()


def mark_replied(conn: sqlite3.Connection, email: str):
    """A genuine reply from the lead -- stop THEIR sequence, do NOT suppress
    the domain (a colleague might still be worth a separate conversation,
    and 'they replied' isn't 'they said no'). A human takes it from here."""
    email = email.strip().lower()
    conn.execute(
        "UPDATE leads SET status = 'replied', updated_at = ? WHERE email = ?",
        (now_iso(), email),
    )
    conn.commit()


def mark_bounced(conn: sqlite3.Connection, email: str):
    email = email.strip().lower()
    conn.execute(
        "UPDATE leads SET status = 'bounced', updated_at = ? WHERE email = ?",
        (now_iso(), email),
    )
    add_suppression(conn, email, "email", "bounced")


def mark_unsubscribed(conn: sqlite3.Connection, email: str, domain_wide: bool = True):
    """Unsubscribe/negative reply -- permanent, and domain-wide by default:
    if one person at a company says stop, don't let a colleague get emailed
    next week by the same campaign."""
    email = email.strip().lower()
    conn.execute(
        "UPDATE leads SET status = 'unsubscribed', updated_at = ? WHERE email = ?",
        (now_iso(), email),
    )
    add_suppression(conn, email, "email", "unsubscribed")
    if domain_wide and "@" in email:
        add_suppression(conn, email.rsplit("@", 1)[-1], "domain", "unsubscribed (colleague requested stop)")
    conn.commit()


def mark_completed(conn: sqlite3.Connection, email: str):
    """Finished the whole sequence with no reply -- stop, don't loop forever."""
    conn.execute(
        "UPDATE leads SET status = 'completed', updated_at = ? WHERE email = ?",
        (now_iso(), email.strip().lower()),
    )
    conn.commit()


def mark_meeting_booked(conn: sqlite3.Connection, email: str):
    """Manual tag -- no calendar API integration exists (that's its own
    paid-tool-replacement scope, not built here). A human marks this after
    a real meeting gets booked; tracked independently of sequence status,
    since a lead can be 'replied' and later have a meeting booked."""
    conn.execute(
        "UPDATE leads SET meeting_booked_at = ?, updated_at = ? WHERE email = ?",
        (now_iso(), now_iso(), email.strip().lower()),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Replies -- Stage 10 feedback loop foundation. Without the actual reply
# TEXT captured and linked to which step/subject it answered, there is
# nothing for a feedback loop to learn from -- reply_watcher.py previously
# only flipped a status flag and discarded the content.
# ---------------------------------------------------------------------------

def save_reply(conn: sqlite3.Connection, email: str, step: int, subject: str, body_text: str) -> int:
    # strip trailing whitespace/newlines -- MIME text bodies routinely carry
    # a trailing newline (verified live: EmailMessage.set_content's own
    # encoding adds one), which is noise for anything downstream (sentiment
    # classification, few-shot prompt injection) that reads this text
    cur = conn.execute(
        "INSERT INTO replies (email, step, subject, body_text, received_at) VALUES (?, ?, ?, ?, ?)",
        (email.strip().lower(), step, subject, (body_text or "").strip(), now_iso()),
    )
    conn.commit()
    return cur.lastrowid


def get_unclassified_replies(conn: sqlite3.Connection) -> list:
    return conn.execute("SELECT * FROM replies WHERE sentiment IS NULL ORDER BY received_at ASC").fetchall()


def update_reply_sentiment(conn: sqlite3.Connection, reply_id: int, sentiment: str, confidence: int = None):
    conn.execute(
        "UPDATE replies SET sentiment = ?, sentiment_confidence = ?, classified_at = ? WHERE id = ?",
        (sentiment, confidence, now_iso(), reply_id),
    )
    conn.commit()


def get_replies_by_sentiment(conn: sqlite3.Connection, sentiment: str) -> list:
    return conn.execute("SELECT * FROM replies WHERE sentiment = ? ORDER BY received_at DESC", (sentiment,)).fetchall()


def reschedule(conn: sqlite3.Connection, email: str, next_send_at: str):
    """Out-of-office or similar -- push the next send out, don't count it
    as a real reply, don't advance the sequence step."""
    conn.execute(
        "UPDATE leads SET next_send_at = ?, updated_at = ? WHERE email = ?",
        (next_send_at, now_iso(), email.strip().lower()),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Mailbox daily send count (warmup cap enforcement)
# ---------------------------------------------------------------------------

def get_mailbox_count_today(conn: sqlite3.Connection, mailbox: str, day: str) -> int:
    row = conn.execute(
        "SELECT count FROM mailbox_daily_counts WHERE mailbox = ? AND day = ?",
        (mailbox, day),
    ).fetchone()
    return row["count"] if row else 0


def increment_mailbox_count(conn: sqlite3.Connection, mailbox: str, day: str):
    conn.execute(
        "INSERT INTO mailbox_daily_counts (mailbox, day, count) VALUES (?, ?, 1) "
        "ON CONFLICT(mailbox, day) DO UPDATE SET count = count + 1",
        (mailbox, day),
    )
    conn.commit()
