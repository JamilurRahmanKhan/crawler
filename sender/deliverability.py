"""
Pre-flight deliverability health checks -- the free equivalent of the
sender-reputation signals a serious sending tool always surfaces. None of
this fakes what only a managed multi-tenant network can do (real inbox
placement testing, warmup-network reputation), but it catches the concrete,
checkable reasons a domain/IP sends to spam: no SPF, no DMARC, or the
sending IP already on a public blacklist. Pure standard DNS queries --
no API key, no account, no cost.

Run this BEFORE a real sending campaign, not just once -- SPF/DMARC records
can change, and a clean IP can get blacklisted mid-campaign if something
upstream (a shared host, a compromised neighbor on the same IP block) goes
wrong.
"""
import re

import dns.resolver

DNS_TIMEOUT_SEC = 5

# Public DNSBL zones safe for LOW-VOLUME lookups (checking your own sending
# IP occasionally) with no API key or account. Spamhaus's ZEN in particular
# has usage policies around high-volume commercial querying -- this is fine
# for "check my own mailbox's IP before a campaign," not for building a
# lookup service that queries thousands of arbitrary IPs per day.
DEFAULT_DNSBL_ZONES = [
    "zen.spamhaus.org",
    "b.barracudacentral.org",
    "dnsbl.sorbs.net",
]

# Common DKIM selectors worth guessing when you don't know your own --
# best-effort, NOT exhaustive. If none of these match, that does not
# necessarily mean DKIM isn't configured, just that the selector wasn't
# one of these common ones (check your ESP's setup docs for the real one).
COMMON_DKIM_SELECTORS = ["google", "default", "selector1", "selector2", "k1", "dkim", "mail"]


def reverse_ip(ip: str) -> str:
    return ".".join(reversed(ip.split(".")))


def check_blacklists(ip: str, zones: list = None) -> list:
    """Returns the list of DNSBL zones that list this IP, [] if clean (or
    if a lookup failed -- a transient DNS error must never be reported as
    'blacklisted', that would be a false positive scaring you off a clean IP)."""
    zones = zones or DEFAULT_DNSBL_ZONES
    reversed_ip = reverse_ip(ip)
    listed = []
    for zone in zones:
        try:
            dns.resolver.resolve(f"{reversed_ip}.{zone}", "A", lifetime=DNS_TIMEOUT_SEC)
            listed.append(zone)  # a resolvable A record here means "listed" -- that's how DNSBLs work
        except dns.resolver.NXDOMAIN:
            continue  # not listed -- the expected, good case
        except Exception:
            continue  # lookup failed for some other reason -- treat as unknown, not listed
    return listed


def _get_txt_strings(domain: str, record_type: str = "TXT") -> list:
    answers = dns.resolver.resolve(domain, record_type, lifetime=DNS_TIMEOUT_SEC)
    texts = []
    for r in answers:
        if hasattr(r, "strings"):
            texts.append(b"".join(r.strings).decode(errors="replace"))
        else:
            texts.append(str(r).strip('"'))
    return texts


def check_spf(domain: str) -> dict:
    try:
        for txt in _get_txt_strings(domain):
            if txt.startswith("v=spf1"):
                return {"found": True, "record": txt}
        return {"found": False, "record": None}
    except Exception:
        return {"found": False, "record": None}


def check_dmarc(domain: str) -> dict:
    try:
        for txt in _get_txt_strings(f"_dmarc.{domain}"):
            if txt.startswith("v=DMARC1"):
                match = re.search(r"p=([a-zA-Z]+)", txt)
                return {"found": True, "record": txt, "policy": match.group(1) if match else None}
        return {"found": False, "record": None, "policy": None}
    except Exception:
        return {"found": False, "record": None, "policy": None}


def check_dkim_selectors(domain: str, selectors: list = None) -> list:
    """Best-effort -- tries common selector names, since the real one isn't
    discoverable from the domain alone without knowing your ESP's config.
    Returns the selectors that DO have a DKIM record; empty list does NOT
    prove DKIM is unconfigured, just that none of the common guesses hit."""
    selectors = selectors or COMMON_DKIM_SELECTORS
    found = []
    for selector in selectors:
        try:
            dns.resolver.resolve(f"{selector}._domainkey.{domain}", "TXT", lifetime=DNS_TIMEOUT_SEC)
            found.append(selector)
        except Exception:
            continue
    return found


def run_health_check(domain: str, ip: str = None) -> dict:
    """One-shot summary for a pre-campaign sanity check."""
    result = {
        "domain": domain,
        "spf": check_spf(domain),
        "dmarc": check_dmarc(domain),
        "dkim_selectors_found": check_dkim_selectors(domain),
    }
    if ip:
        result["blacklists"] = check_blacklists(ip)
    return result
