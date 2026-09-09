"""
Main sending loop. Free replacement for Smartlead's send-safely-at-scale job:
respects the warmup ramp, enforces strict sequence order, threads follow-ups
as replies, checks suppression before every single send, and never sends
past today's mailbox cap.

Input contract (decoupled from however the content got drafted -- Stage 5,
or a manual batch today): a JSON file, a list of objects:
    [
      {"to_email": "jane@acme.com", "to_name": "Jane Diaz",
       "subject": "quick question", "body": "...", "step": 1},
      ...
    ]

Usage:
    python sender.py --queue queue.json --dry-run
    python sender.py --queue queue.json --limit 15
    python sender.py --queue queue.json

--dry-run builds every message and logs what WOULD be sent, without opening
any SMTP connection. Always run this first on a new queue file.
"""
import argparse
import json
import logging
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import db as db_module
import deliverability
import mailboxes as mailboxes_module
import smtp_sender
import warmup
from config import Config
from mailboxes import Mailbox

log = logging.getLogger("sender")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                     handlers=[logging.StreamHandler(sys.stdout)])

MAX_SEQUENCE_STEP = 4
# days to wait before the NEXT step after sending step N (E1->3d->E2->4d->E3->5d->E4->stop)
SEQUENCE_DELAY_DAYS = {1: 3, 2: 4, 3: 5}


def load_queue(path: str) -> list:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise SystemExit(f"{path} must contain a JSON array of message objects")
    required = {"to_email", "subject", "body", "step"}
    for i, item in enumerate(data):
        missing = required - item.keys()
        if missing:
            raise SystemExit(f"queue item {i} missing fields: {missing}")
    return data


def get_thread_headers(conn, email: str) -> tuple:
    """For a follow-up step, thread it as a reply to step-1's message so the
    sequence shows up as one conversation, not four separate cold emails."""
    row = conn.execute(
        "SELECT message_id FROM send_log WHERE email = ? AND step = 1 AND status = 'sent' "
        "ORDER BY sent_at ASC LIMIT 1",
        (email.strip().lower(),),
    ).fetchone()
    if row and row["message_id"]:
        return row["message_id"], row["message_id"]
    return None, None


def next_send_at_for_step(step: int) -> str:
    delay_days = SEQUENCE_DELAY_DAYS.get(step)
    if delay_days is None:
        return None
    return (datetime.now(timezone.utc) + timedelta(days=delay_days)).isoformat()


def _single_mailbox_from_config() -> Mailbox:
    """Backward-compat / simple case: one mailbox driven by .env, same
    behavior as before mailbox rotation existed."""
    return Mailbox(
        name=Config.MAILBOX_NAME, smtp_host=Config.SMTP_HOST, smtp_port=Config.SMTP_PORT,
        smtp_user=Config.SMTP_USER, smtp_password=Config.SMTP_PASSWORD,
        imap_host=Config.IMAP_HOST, imap_port=Config.IMAP_PORT,
        from_name=Config.FROM_NAME, reply_to=Config.REPLY_TO,
        warmup_start_date=Config.WARMUP_START_DATE, daily_cap_override=Config.DAILY_CAP_OVERRIDE,
    )


def run_preflight_check(mailboxes: list, skip: bool = False):
    """Warns (never blocks -- a DNS hiccup shouldn't stop a real campaign)
    if a mailbox's sending domain is missing SPF or DMARC. Run before a
    real batch, same spirit as the deliverability signals a paid sending
    tool always surfaces. See deliverability.py for the checks themselves."""
    if skip:
        return
    checked_domains = set()
    for mb in mailboxes:
        if not mb.smtp_user or "@" not in mb.smtp_user:
            continue
        domain = mb.smtp_user.split("@", 1)[-1]
        if domain in checked_domains:
            continue
        checked_domains.add(domain)

        result = deliverability.run_health_check(domain)
        if not result["spf"]["found"]:
            log.warning(f"[preflight] {domain}: no SPF record found -- deliverability risk, "
                        f"see deliverability.py / your DNS provider")
        if not result["dmarc"]["found"]:
            log.warning(f"[preflight] {domain}: no DMARC record found -- deliverability risk")


def run_batch(queue: list, conn, mailboxes: list = None, dry_run: bool = False, limit: int = None) -> dict:
    """mailboxes: list of Mailbox (see mailboxes.py). Defaults to a single
    mailbox built from Config (.env) if not given -- the pre-rotation
    behavior, unchanged, for callers/tests that don't need multiple mailboxes."""
    summary = {"sent": 0, "skipped_suppressed": 0, "skipped_out_of_order": 0,
               "skipped_inactive": 0, "skipped_cap_reached": 0, "failed": 0}

    using_pool = mailboxes is not None
    mailboxes = mailboxes or [_single_mailbox_from_config()]
    today_str = datetime.now(timezone.utc).date().isoformat()
    for mb in mailboxes:
        sent_today = db_module.get_mailbox_count_today(conn, mb.name, today_str)
        stage = warmup.warmup_stage_label(mb.warmup_start_date)
        log.info(f"Mailbox {mb.name}: {stage}, cap={mb.daily_cap()}/day, already sent today={sent_today}")

    for item in queue:
        if limit is not None and summary["sent"] >= limit:
            log.info(f"Reached --limit {limit}, stopping this run.")
            break

        to_email = item["to_email"].strip().lower()
        step = int(item["step"])

        db_module.upsert_lead(conn, to_email, name=item.get("to_name"))

        if db_module.is_suppressed(conn, to_email):
            log.info(f"[skip] {to_email}: suppressed")
            summary["skipped_suppressed"] += 1
            continue

        lead = db_module.get_lead(conn, to_email)
        if lead["status"] != "active":
            log.info(f"[skip] {to_email}: lead status is '{lead['status']}', not active")
            summary["skipped_inactive"] += 1
            continue

        if step != lead["sequence_step"] + 1:
            log.warning(f"[skip] {to_email}: queue has step {step} but lead is at "
                        f"step {lead['sequence_step']} -- sequence must proceed in order")
            summary["skipped_out_of_order"] += 1
            continue

        mb = mailboxes_module.pick_mailbox(conn, mailboxes, today_str, db_module.get_mailbox_count_today)
        if mb is None:
            log.warning(f"All {len(mailboxes)} mailbox(es) at their daily cap -- stopping this run. "
                        f"Remaining queue items will be picked up on the next run.")
            summary["skipped_cap_reached"] += len(queue) - queue.index(item)
            break

        in_reply_to, references = (None, None) if step == 1 else get_thread_headers(conn, to_email)

        if dry_run:
            log.info(f"[dry-run] would send step {step} to {to_email} via {mb.name}: \"{item['subject']}\"")
            summary["sent"] += 1
            continue

        if using_pool:
            Config.require_compliance_ready()  # per-mailbox creds already validated at load time
        else:
            Config.require_send_ready()
        success, result = smtp_sender.send_email(
            smtp_host=mb.smtp_host, smtp_port=mb.smtp_port,
            smtp_user=mb.smtp_user, smtp_password=mb.smtp_password,
            from_name=mb.from_name, to_addr=to_email,
            subject=item["subject"], body=item["body"], reply_to=mb.reply_to,
            physical_address=Config.PHYSICAL_ADDRESS, unsubscribe_text=Config.UNSUBSCRIBE_TEXT,
            in_reply_to=in_reply_to, references=references,
            unsubscribe_mailto=mb.reply_to, unsubscribe_url=Config.UNSUBSCRIBE_URL,
        )

        if success:
            message_id = result
            next_send_at = next_send_at_for_step(step)
            db_module.mark_sent(conn, to_email, step, item["subject"], message_id, mb.name, next_send_at)
            if step >= MAX_SEQUENCE_STEP or next_send_at is None:
                db_module.mark_completed(conn, to_email)
            db_module.increment_mailbox_count(conn, mb.name, today_str)
            summary["sent"] += 1
            sent_today = db_module.get_mailbox_count_today(conn, mb.name, today_str)
            log.info(f"[sent] step {step} -> {to_email} via {mb.name} ({sent_today}/{mb.daily_cap()} today)")
            time.sleep(random.uniform(Config.MIN_SEND_DELAY_SEC, Config.MAX_SEND_DELAY_SEC))
        else:
            db_module.mark_send_failed(conn, to_email, step, result, mb.name)
            summary["failed"] += 1
            log.error(f"[failed] step {step} -> {to_email} via {mb.name}: {result}")

    return summary


def main():
    ap = argparse.ArgumentParser(description="Free Smartlead-replacement sender.")
    ap.add_argument("--queue", required=True, help="JSON file of ready-to-send messages")
    ap.add_argument("--mailboxes", help="JSON file of multiple mailbox configs for rotation "
                                         "(see mailboxes.example.json). Omit to use single-mailbox .env config.")
    ap.add_argument("--dry-run", action="store_true", help="log what would be sent, send nothing")
    ap.add_argument("--limit", type=int, help="max sends this invocation (independent of daily cap)")
    ap.add_argument("--skip-preflight", action="store_true",
                     help="skip the SPF/DMARC pre-flight check (runs by default before a real send)")
    args = ap.parse_args()

    queue = load_queue(args.queue)
    conn = db_module.get_connection(Config.DB_PATH)
    db_module.init_db(conn)

    mailbox_pool = mailboxes_module.load_mailboxes(args.mailboxes) if args.mailboxes else None

    if not args.dry_run:
        if mailbox_pool is None:
            Config.require_send_ready()
        else:
            Config.require_compliance_ready()
        run_preflight_check(mailbox_pool or [_single_mailbox_from_config()], skip=args.skip_preflight)

    summary = run_batch(queue, conn, mailboxes=mailbox_pool, dry_run=args.dry_run, limit=args.limit)
    log.info(f"Done. {summary}")


if __name__ == "__main__":
    main()
