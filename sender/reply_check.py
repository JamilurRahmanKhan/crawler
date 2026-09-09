"""
Run one IMAP polling pass for replies/bounces/unsubscribes/OOO. Run this on
a schedule (cron, Task Scheduler, whatever) every 15-30 min, or manually.

Usage:
    python reply_check.py
"""
import logging
import sys

import db as db_module
import reply_watcher
from config import Config

log = logging.getLogger("reply_check")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                     handlers=[logging.StreamHandler(sys.stdout)])


def main():
    if not Config.IMAP_USER or not Config.IMAP_PASSWORD:
        raise SystemExit("Missing IMAP_USER/IMAP_PASSWORD (or SMTP_USER/SMTP_PASSWORD as fallback). See .env.example")

    conn = db_module.get_connection(Config.DB_PATH)
    db_module.init_db(conn)

    log.info(f"Connecting to {Config.IMAP_HOST}:{Config.IMAP_PORT} as {Config.IMAP_USER}...")
    imap_conn = reply_watcher.connect_imap(Config.IMAP_HOST, Config.IMAP_PORT, Config.IMAP_USER, Config.IMAP_PASSWORD)
    try:
        summary = reply_watcher.poll_and_process(conn, imap_conn, log=log.info)
    finally:
        try:
            imap_conn.logout()
        except Exception:
            pass

    log.info(f"Done. {summary}")
    if summary["reply"] > 0:
        log.info(f"*** {summary['reply']} genuine repl{'y' if summary['reply']==1 else 'ies'} need human review ***")


if __name__ == "__main__":
    main()
