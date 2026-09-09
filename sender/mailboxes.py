"""
Multi-mailbox rotation -- the free replacement for Smartlead's "spread sends
across many mailboxes so one account doesn't get flagged" job. Previously
this engine only drove one mailbox per run (a documented gap: "run separate
configs per mailbox, split queue yourself"). This makes it automatic.

Each mailbox has its OWN warmup clock and its OWN daily cap -- a 3-week-old
mailbox and a brand-new one sending side by side each ramp on their own
schedule, not lock-stepped to whichever is furthest behind.

Load-balances rather than strict round-robin: every pick goes to whichever
mailbox has the most REMAINING capacity today. That naturally keeps usage
proportional to each mailbox's own warmup stage instead of burning through
a newer, lower-cap mailbox's daily budget at the same rate as a fully-warmed
one just because "it's its turn."
"""
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import warmup


@dataclass
class Mailbox:
    name: str  # used as the identity key for daily-count tracking and send_log
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    imap_host: str
    imap_port: int
    from_name: str
    reply_to: str
    warmup_start_date: date
    daily_cap_override: int = None

    def daily_cap(self, today: date = None) -> int:
        if self.daily_cap_override is not None:
            return self.daily_cap_override
        return warmup.compute_daily_cap(self.warmup_start_date, today)


def load_mailboxes(path: str) -> list:
    """mailboxes.json: a JSON array of mailbox configs. See mailboxes.example.json."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not raw:
        raise SystemExit(f"{path} must contain a non-empty JSON array of mailbox configs")

    mailboxes = []
    for i, m in enumerate(raw):
        for field in ("name", "smtp_host", "smtp_user", "smtp_password", "warmup_start_date"):
            if not m.get(field):
                raise SystemExit(f"mailbox config #{i} missing required field: {field}")
        mailboxes.append(Mailbox(
            name=m["name"],
            smtp_host=m["smtp_host"],
            smtp_port=m.get("smtp_port", 587),
            smtp_user=m["smtp_user"],
            smtp_password=m["smtp_password"],
            imap_host=m.get("imap_host") or m["smtp_host"].replace("smtp.", "imap.", 1),
            imap_port=m.get("imap_port", 993),
            from_name=m.get("from_name", ""),
            reply_to=m.get("reply_to") or m["smtp_user"],
            warmup_start_date=date.fromisoformat(m["warmup_start_date"]),
            daily_cap_override=m.get("daily_cap_override"),
        ))

    names = [m.name for m in mailboxes]
    if len(names) != len(set(names)):
        raise SystemExit(f"duplicate mailbox names in {path} -- names must be unique (used as the tracking key)")
    return mailboxes


def pick_mailbox(conn, mailboxes: list, today_str: str, get_count_fn):
    """Returns the Mailbox with the most remaining capacity today, or None
    if every mailbox in the pool is at (or over) its own daily cap.
    `get_count_fn` is db.get_mailbox_count_today, injected so this stays
    testable without a real sqlite connection wired through every test."""
    best = None
    best_remaining = 0
    for mb in mailboxes:
        cap = mb.daily_cap()
        sent = get_count_fn(conn, mb.name, today_str)
        remaining = cap - sent
        if remaining > best_remaining:
            best_remaining = remaining
            best = mb
    return best
