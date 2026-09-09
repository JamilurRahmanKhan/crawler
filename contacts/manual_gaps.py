"""
Human-in-the-loop fallback for the domains automation genuinely can't solve.

This is the honest answer to "make it 100% like Apollo": there isn't a code
path left that closes that gap for free (see contacts.py's module docstring
and README for what was tried -- OpenCorporates, search-engine fallback --
and why "100%" is a data problem, not a code problem). What DOES work: a
person manually looks up the hard cases using the same convenience
search-URL pattern the automated tool already builds, then feeds results
back in through this file, and downstream stages (drafting, sending) see it
exactly like any other found contact -- no special-casing needed.

Workflow:
    1. python manual_gaps.py export --crawl-dir ../crawler/output --out gaps.csv
       Lists every domain the automation didn't confidently solve, with a
       company-name guess and ready-made Google/LinkedIn search links.

    2. A human (you, or a cheap VA) opens gaps.csv and fills in
       found_name / found_title / found_email for whatever they can find.
       Leave blank if genuinely not findable -- never guess. Mark
       verified_yn=Y only if you actually confirmed the person currently
       works there (e.g. LinkedIn profile shows current employer match).

    3. python manual_gaps.py import --crawl-dir ../crawler/output --in gaps.csv
       Merges filled rows into each domain's contacts.json as a
       source="manual_lookup" contact -- confidence="high" if verified_yn=Y,
       "medium" otherwise (still a human find, just not independently
       confirmed against a second source).

Cost model: zero software cost. Real cost is human time -- budget roughly
2-4 minutes per domain for a person who knows what they're looking for.
"""
import argparse
import csv
import json
import re
from pathlib import Path
from urllib.parse import quote_plus


def is_gap(contacts_data: dict) -> tuple:
    """Returns (is_gap, reason). reason: 'no_contact' | 'low_confidence' | ''."""
    status = contacts_data.get("status")
    if status in ("no_contacts_found", "no_crawl_data"):
        return True, "no_contact"
    contacts = contacts_data.get("contacts", [])
    if contacts and all(c.get("confidence") == "low" for c in contacts):
        return True, "low_confidence"
    return False, ""


def company_name_guess(domain: str) -> str:
    """'stripe.com' -> 'Stripe', 'acme-widgets.com' -> 'Acme Widgets' --
    display-friendly, for use in a search query a human will read."""
    base = domain.split(".")[0]
    words = re.split(r"[-_]", base)
    return " ".join(w.capitalize() for w in words if w)


def google_search_url(query: str) -> str:
    return f"https://www.google.com/search?q={quote_plus(query)}"


def build_gap_row(domain: str, contacts_data: dict, reason: str) -> dict:
    company = company_name_guess(domain)
    existing = contacts_data.get("contacts", [])
    best = existing[0] if existing else {}
    return {
        "domain": domain,
        "company_name_guess": company,
        "gap_reason": reason,
        "existing_best_name": best.get("matched_name") or "",
        "existing_best_email": best.get("email") or "",
        "google_search_url": google_search_url(f'"{company}" founder OR CEO OR "head of sales"'),
        "linkedin_search_url": google_search_url(f'"{company}" {domain} site:linkedin.com/in'),
        "found_name": "",
        "found_title": "",
        "found_email": "",
        "verified_yn": "",
        "notes": "",
    }


def find_gap_domains(crawl_dir: Path) -> list:
    """Returns [(domain, contacts_data, reason), ...] for every domain
    that's a gap. Domains with no contacts.json at all (crawl never ran, or
    Stage 4 hasn't been run yet) are skipped -- run contacts.py first."""
    gaps = []
    for domain_dir in sorted(crawl_dir.iterdir()):
        contacts_path = domain_dir / "contacts.json"
        if not domain_dir.is_dir() or not contacts_path.exists():
            continue
        data = json.loads(contacts_path.read_text(encoding="utf-8"))
        gap, reason = is_gap(data)
        if gap:
            gaps.append((domain_dir.name, data, reason))
    return gaps


def export_gaps_csv(crawl_dir: Path, out_path: Path) -> int:
    gaps = find_gap_domains(crawl_dir)
    fieldnames = ["domain", "company_name_guess", "gap_reason", "existing_best_name",
                  "existing_best_email", "google_search_url", "linkedin_search_url",
                  "found_name", "found_title", "found_email", "verified_yn", "notes"]
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for domain, data, reason in gaps:
            w.writerow(build_gap_row(domain, data, reason))
    return len(gaps)


def import_manual_csv(crawl_dir: Path, in_path: Path) -> dict:
    summary = {"imported": 0, "skipped_blank": 0, "skipped_no_domain_dir": 0}
    with open(in_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    for row in rows:
        domain = (row.get("domain") or "").strip()
        found_email = (row.get("found_email") or "").strip()
        if not found_email:
            summary["skipped_blank"] += 1
            continue

        contacts_path = crawl_dir / domain / "contacts.json"
        if not contacts_path.exists():
            summary["skipped_no_domain_dir"] += 1
            continue

        data = json.loads(contacts_path.read_text(encoding="utf-8"))
        verified = (row.get("verified_yn") or "").strip().upper() == "Y"
        new_contact = {
            "email": found_email,
            "role": "other",
            "matched_name": (row.get("found_name") or "").strip() or None,
            "matched_title": (row.get("found_title") or "").strip() or None,
            "source": "manual_lookup",
            "verification": "human_verified" if verified else "human_found_unverified",
            "verification_note": row.get("notes") or "",
            "confidence": "high" if verified else "medium",
            "linkedin_lookup_url": None,
        }

        existing_emails = {c.get("email") for c in data.get("contacts", [])}
        if found_email not in existing_emails:
            data.setdefault("contacts", []).append(new_contact)
        data["status"] = "ok"

        contacts_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        summary["imported"] += 1

    return summary


def main():
    ap = argparse.ArgumentParser(description="Human-in-the-loop fallback for contact-discovery gaps.")
    sub = ap.add_subparsers(dest="command", required=True)

    export_ap = sub.add_parser("export", help="list domains needing manual lookup")
    export_ap.add_argument("--crawl-dir", default="../crawler/output")
    export_ap.add_argument("--out", default="gaps.csv")

    import_ap = sub.add_parser("import", help="merge a filled-in gaps CSV back in")
    import_ap.add_argument("--crawl-dir", default="../crawler/output")
    import_ap.add_argument("--in", dest="in_path", required=True)

    args = ap.parse_args()
    crawl_dir = Path(args.crawl_dir)
    if not crawl_dir.exists():
        raise SystemExit(f"--crawl-dir not found: {crawl_dir}")

    if args.command == "export":
        count = export_gaps_csv(crawl_dir, Path(args.out))
        print(f"Exported {count} gap domain(s) to {args.out}")
    elif args.command == "import":
        summary = import_manual_csv(crawl_dir, Path(args.in_path))
        print(f"Done. {summary}")


if __name__ == "__main__":
    main()
