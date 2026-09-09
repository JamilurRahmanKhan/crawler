"""
Campaign reporting -- the free equivalent of Smartlead's dashboard basics.
All from data db.py was already tracking; this just aggregates it. No new
dependency, no external service.

Usage:
    python reports.py
    python reports.py --db sender_state.db
"""
import argparse
import sqlite3

import db as db_module
from config import Config


def campaign_summary(conn: sqlite3.Connection) -> dict:
    lead_status_breakdown = {}
    for row in conn.execute("SELECT status, COUNT(*) as c FROM leads GROUP BY status"):
        lead_status_breakdown[row["status"]] = row["c"]
    total_leads = sum(lead_status_breakdown.values())

    total_sent = conn.execute("SELECT COUNT(*) as c FROM send_log WHERE status = 'sent'").fetchone()["c"]
    total_failed = conn.execute("SELECT COUNT(*) as c FROM send_log WHERE status = 'failed'").fetchone()["c"]

    replied = lead_status_breakdown.get("replied", 0)
    bounced = lead_status_breakdown.get("bounced", 0)
    unsubscribed = lead_status_breakdown.get("unsubscribed", 0)

    return {
        "total_leads": total_leads,
        "lead_status_breakdown": lead_status_breakdown,
        "total_sent": total_sent,
        "total_failed": total_failed,
        "replied": replied,
        "bounced": bounced,
        "unsubscribed": unsubscribed,
        "reply_rate": round(replied / total_sent, 4) if total_sent else 0,
        "bounce_rate": round(bounced / total_sent, 4) if total_sent else 0,
    }


def mailbox_breakdown(conn: sqlite3.Connection) -> list:
    rows = conn.execute(
        "SELECT mailbox, "
        "SUM(CASE WHEN status = 'sent' THEN 1 ELSE 0 END) as sent, "
        "SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed "
        "FROM send_log GROUP BY mailbox ORDER BY sent DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def daily_send_counts(conn: sqlite3.Connection, days: int = 14) -> list:
    rows = conn.execute(
        "SELECT day, SUM(count) as total FROM mailbox_daily_counts "
        "GROUP BY day ORDER BY day DESC LIMIT ?",
        (days,),
    ).fetchall()
    return [dict(r) for r in rows]


def print_report(conn: sqlite3.Connection):
    summary = campaign_summary(conn)
    print("=== Campaign Summary ===")
    print(f"Total leads:        {summary['total_leads']}")
    for status, count in sorted(summary["lead_status_breakdown"].items()):
        print(f"  {status:15s} {count}")
    print(f"Total sent:         {summary['total_sent']}")
    print(f"Total failed:       {summary['total_failed']}")
    print(f"Replied:            {summary['replied']} ({summary['reply_rate']:.1%})")
    print(f"Bounced:            {summary['bounced']} ({summary['bounce_rate']:.1%})")
    print(f"Unsubscribed:       {summary['unsubscribed']}")

    print("\n=== By Mailbox ===")
    for row in mailbox_breakdown(conn):
        print(f"  {row['mailbox']:30s} sent={row['sent'] or 0:5d}  failed={row['failed'] or 0}")

    print("\n=== Daily Send Volume (last 14 days) ===")
    for row in daily_send_counts(conn):
        print(f"  {row['day']}  {row['total']}")


def main():
    ap = argparse.ArgumentParser(description="Campaign reporting -- free Smartlead-dashboard equivalent.")
    ap.add_argument("--db", default=Config.DB_PATH, help="sqlite db path (default: from .env / sender_state.db)")
    args = ap.parse_args()

    conn = db_module.get_connection(args.db)
    db_module.init_db(conn)
    print_report(conn)


if __name__ == "__main__":
    main()
