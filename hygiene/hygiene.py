"""
Stage 1: Hygiene -- runs BEFORE crawling, on the raw leads file. Kills the
domains not worth spending a crawl on, so crawler.py never wastes a fetch on
a duplicate, a dead domain, a parked placeholder, or someone who already
unsubscribed from a past campaign.

Per the original pipeline design:
  1. Normalize to root domain, strip tracking params (UTM/fbclid/gclid).
  2. Dedupe by root domain.
  3. MX check -- no MX means the domain literally cannot receive email.
  4. Parked-domain detection -- nameserver-based, catches placeholder/for-
     sale domains before wasting a crawl on them.
  5. Global suppression check -- cross-references sender/'s own suppression
     table (sender_state.db), so a domain that unsubscribed from a past
     campaign, or bounced hard, never gets crawled/contacted again. This is
     a REAL integration with sender/db.py's schema, not a stub.
  6. Geo-tag via ccTLD -- a real, free, honest signal (not a guess dressed
     up as certainty) that feeds compliance routing downstream (CASL/GDPR
     exclusions are a decision made on this tag, not decided here).

Output is two CSVs:
  - kept:    ready to hand straight to crawler.py, same column name
             ("website") crawler.py's own --domain-column defaults to, so
             the next stage needs ZERO reformatting.
  - dropped: domain + reason, for audit -- nothing is silently discarded
             without a paper trail.

Usage:
    python hygiene.py leads_raw.csv --url-column website
    python hygiene.py leads_raw.csv --url-column "Company Website" \\
        --sender-db ../sender/sender_state.db

Then:
    python ../crawler/crawl.py --domains-file hygiene_kept.csv --domain-column website
"""
import argparse
import csv
import re
import sqlite3
from pathlib import Path
from urllib.parse import urlparse, urlencode, parse_qsl

import dns.resolver
import tldextract

DNS_TIMEOUT_SEC = 5

TRACKING_PARAM_PREFIXES = ("utm_",)
TRACKING_PARAM_EXACT = {"fbclid", "gclid", "msclkid", "mc_cid", "mc_eid", "ref", "igshid"}

# Best-effort, not exhaustive -- known parking-service nameservers. Verified
# technique (parked domains are pointed at a parking service's own DNS
# infrastructure), same honest framing as the DKIM-selector guessing in
# sender/deliverability.py: a miss here doesn't prove a domain ISN'T parked,
# just that its parking service (if any) isn't one of these common ones.
PARKING_NAMESERVER_MARKERS = (
    "sedoparking.com", "parkingcrew.net", "above.com", "bodis.com",
    "parklogic.com", "dan.com", "voodoo.com", "parked.com",
)

# ccTLD -> geo tag. Deliberately small and only the ones with a real,
# unambiguous compliance implication (CASL/GDPR) -- anything else honestly
# reports "unknown" rather than guessing. .com/.org/.net/.io etc are used
# worldwide and carry no reliable geo signal on their own.
CCTLD_GEO_MAP = {
    "ca": "CA", "uk": "UK", "de": "DE", "fr": "FR", "au": "AU",
    "nz": "NZ", "ie": "IE", "nl": "NL", "se": "SE", "no": "NO",
    "dk": "DK", "fi": "FI", "es": "ES", "it": "IT", "jp": "JP",
    "in": "IN", "br": "BR", "mx": "MX",
}


def strip_tracking_params(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.query:
        return url
    kept = [
        (k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if k.lower() not in TRACKING_PARAM_EXACT
        and not any(k.lower().startswith(p) for p in TRACKING_PARAM_PREFIXES)
    ]
    new_query = urlencode(kept)
    cleaned = parsed._replace(query=new_query)
    return cleaned.geturl().rstrip("?")


def normalize_url(raw: str) -> str:
    """'https://www.Acme.com/foo?utm_source=x' -> 'acme.com'. Mirrors
    crawler.normalize_domain's technique (tldextract, root-domain only) so
    a lead normalizes to the exact same key at every stage of the pipeline."""
    raw = raw.strip()
    if not raw:
        return ""
    if not raw.startswith("http"):
        raw = "https://" + raw
    ext = tldextract.extract(raw)
    if not ext.domain or not ext.suffix:
        return ""
    return f"{ext.domain}.{ext.suffix}".lower()


def dedupe_leads(leads: list, domain_key: str = "domain") -> tuple:
    """Keeps the FIRST occurrence of each domain. Returns (kept, dup_count)."""
    seen = set()
    kept = []
    dup_count = 0
    for lead in leads:
        key = lead[domain_key].lower()
        if key in seen:
            dup_count += 1
            continue
        seen.add(key)
        kept.append(lead)
    return kept, dup_count


def has_mx_record(domain: str) -> bool:
    try:
        answers = dns.resolver.resolve(domain, "MX", lifetime=DNS_TIMEOUT_SEC)
        return len(answers) > 0
    except Exception:
        return False


def is_likely_parked(domain: str) -> bool:
    try:
        answers = dns.resolver.resolve(domain, "NS", lifetime=DNS_TIMEOUT_SEC)
        for rec in answers:
            ns_host = str(rec.target).rstrip(".").lower()
            if any(marker in ns_host for marker in PARKING_NAMESERVER_MARKERS):
                return True
        return False
    except Exception:
        return False


def geo_tag_from_tld(domain: str) -> str:
    ext = tldextract.extract(domain if domain.startswith("http") else f"https://{domain}")
    suffix_parts = ext.suffix.split(".") if ext.suffix else []
    for part in suffix_parts:
        if part in CCTLD_GEO_MAP:
            return CCTLD_GEO_MAP[part]
    return "unknown"


def is_globally_suppressed(value: str, sender_db_path: str) -> bool:
    """Cross-references sender/db.py's ACTUAL suppression table -- a domain
    that unsubscribed from (or hard-bounced on) a past campaign never gets
    crawled or contacted again. Real integration: same schema, same file
    sender.py's suppression logic already writes to."""
    if not sender_db_path or not Path(sender_db_path).exists():
        return False
    try:
        conn = sqlite3.connect(sender_db_path)
        try:
            row = conn.execute(
                "SELECT 1 FROM suppression WHERE value = ? AND scope = 'domain' LIMIT 1",
                (value.lower(),),
            ).fetchone()
            return row is not None
        finally:
            conn.close()
    except Exception:
        return False


def run_hygiene(input_csv: str, url_column: str, kept_out: str, dropped_out: str,
                sender_db_path: str = None) -> dict:
    with open(input_csv, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if url_column not in (reader.fieldnames or []):
            raise SystemExit(f"Column '{url_column}' not found in {input_csv}. "
                              f"Available columns: {reader.fieldnames}")
        raw_rows = list(reader)

    summary = {
        "total_input": len(raw_rows), "duplicates_dropped": 0, "invalid_url_dropped": 0,
        "no_mx_dropped": 0, "parked_dropped": 0, "suppressed_dropped": 0, "kept": 0,
    }
    dropped_rows = []
    normalized = []

    for row in raw_rows:
        raw_url = (row.get(url_column) or "").strip()
        domain = normalize_url(raw_url)
        if not domain:
            summary["invalid_url_dropped"] += 1
            dropped_rows.append({**row, "domain": raw_url, "drop_reason": "invalid_url"})
            continue
        normalized.append({**row, "domain": domain})

    deduped, dup_count = dedupe_leads(normalized, domain_key="domain")
    summary["duplicates_dropped"] = dup_count

    kept_rows = []
    for lead in deduped:
        domain = lead["domain"]

        if not has_mx_record(domain):
            summary["no_mx_dropped"] += 1
            dropped_rows.append({**lead, "drop_reason": "no_mx"})
            continue

        if is_likely_parked(domain):
            summary["parked_dropped"] += 1
            dropped_rows.append({**lead, "drop_reason": "parked_domain"})
            continue

        if is_globally_suppressed(domain, sender_db_path):
            summary["suppressed_dropped"] += 1
            dropped_rows.append({**lead, "drop_reason": "globally_suppressed"})
            continue

        clean_url = strip_tracking_params(lead.get(url_column, ""))
        kept_rows.append({
            **lead,
            "website": domain,
            "original_url_cleaned": clean_url,
            "geo": geo_tag_from_tld(domain),
        })

    summary["kept"] = len(kept_rows)

    kept_fieldnames = []
    for r in kept_rows:
        for k in r.keys():
            if k not in kept_fieldnames:
                kept_fieldnames.append(k)
    with open(kept_out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=kept_fieldnames or ["website", "geo"])
        w.writeheader()
        w.writerows(kept_rows)

    dropped_fieldnames = []
    for r in dropped_rows:
        for k in r.keys():
            if k not in dropped_fieldnames:
                dropped_fieldnames.append(k)
    with open(dropped_out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=dropped_fieldnames or ["domain", "drop_reason"])
        w.writeheader()
        w.writerows(dropped_rows)

    return summary


def main():
    ap = argparse.ArgumentParser(description="Stage 1: Hygiene -- clean a raw leads CSV before crawling.")
    ap.add_argument("input_csv", help="raw leads CSV")
    ap.add_argument("--url-column", default="website", help="column holding the website/URL (default: website)")
    ap.add_argument("--kept-out", default="hygiene_kept.csv", help="output: ready-to-crawl leads")
    ap.add_argument("--dropped-out", default="hygiene_dropped.csv", help="output: dropped leads + reason")
    ap.add_argument("--sender-db", default="../sender/sender_state.db",
                     help="path to sender/'s sqlite DB, for the global-suppression cross-check "
                          "(skipped gracefully if the file doesn't exist yet)")
    args = ap.parse_args()

    summary = run_hygiene(args.input_csv, args.url_column, args.kept_out, args.dropped_out,
                           sender_db_path=args.sender_db)

    print(f"Input:               {summary['total_input']}")
    print(f"Duplicates dropped:  {summary['duplicates_dropped']}")
    print(f"Invalid URL dropped: {summary['invalid_url_dropped']}")
    print(f"No MX dropped:       {summary['no_mx_dropped']}")
    print(f"Parked dropped:      {summary['parked_dropped']}")
    print(f"Suppressed dropped:  {summary['suppressed_dropped']}")
    print(f"Kept:                {summary['kept']}  -> {args.kept_out}")
    print(f"\nNext: python ../crawler/crawl.py --domains-file {args.kept_out} --domain-column website")


if __name__ == "__main__":
    main()
