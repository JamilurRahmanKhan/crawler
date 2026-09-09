"""
Stage 4: Contact discovery + verification -- free replacement for Apollo/Findymail.

Consumes crawl_result.json files produced by ../crawler/crawl.py (including its
`team_members` field -- names+titles extracted structurally from raw HTML, since
trafilatura's prose text silently drops names inside card/button UI elements;
see the crawler's own docstring for why). Does NOT re-crawl anything.

What this replicates from Apollo, and how:
  1. Contact ranking: classifies every scraped email by role (founder/sales >
     personal > generic info@ > support/hr, worst last) so the best contact
     for a SALES pitch gets picked, not just the first email found.
  2. Name+title data: uses the crawler's structural team-page extraction
     (primary) plus a prose regex over about/contact text (supplementary) --
     this IS the "who works here, what's their title" data Apollo sells.
  3. Apollo's actual core trick -- the email finder: given a name and a domain,
     Apollo mostly just applies a known convention ({first}.{last}@, etc.) that
     it learned from examples. This file does the same, for free: it derives
     the domain's own convention from any confirmed real name+email pair found
     on-site, then APPLIES that pattern to every other named person who has no
     directly-published email (see detect_pattern/apply_pattern). When no
     pattern can be derived, it falls back to a bounded brute-force of common
     conventions -- but ONLY verifies and keeps one if SMTP confirms it, so
     this never floods output with unverifiable noise.
  4. Verifies deliverability for free: MX record lookup + an SMTP RCPT probe
     (best-effort -- see LIMITATIONS below, this is NOT as reliable as a paid
     verifier, and is explicitly labeled as such in the output).
  5. Detects catch-all domains (accept literally any address) ONCE per domain
     (cached) so a "valid" result isn't mistaken for "this specific mailbox
     definitely exists" -- and so brute-force guessing on a catch-all domain
     is correctly recognized as unverifiable rather than wrongly trusted.
  6. Falls back to common-prefix guesses (info@, sales@, contact@) when zero
     emails were found on-site at all, run through the same verification.
  7. Never outputs an address that verified as hard-invalid (550-class reject).
  8. Caps contacts per domain (default 2) -- matches the design's "don't email
     5 people at one company" rule.
  9. Structured output fields (role/seniority/department-ish via `role`) so
     downstream drafting can address "the sales decision-maker" specifically,
     the way an Apollo title/seniority filter would let you target one.

Deliberately NOT replicated -- flagged, not silently skipped:
  - LinkedIn profile data. Apollo has it; this does not attempt to scrape
    LinkedIn. Automated scraping of LinkedIn's platform is against its Terms
    of Service, LinkedIn actively pursues legal action against scrapers
    (hiQ Labs, Mantheos, and others), and it risks the scraping IP / any
    linked account being banned. That risk isn't worth taking to save an
    Apollo subscription. See `linkedin_lookup_url()` below for the
    alternative actually included: a plain search-URL builder for a HUMAN
    to manually glance at, not automated data extraction.
  - Direct-dial phone numbers. Out of scope for a cold EMAIL campaign.
  - A cross-company contact database. Apollo's data comes from aggregating
    across millions of companies; this only ever sees what one company's own
    website publishes. This is the hard ceiling on hit-rate vs. Apollo --
    stated plainly in LIMITATIONS below, not hidden.

LIMITATIONS (be honest with yourself before trusting this over Apollo):
  - No external cross-company database -- see above. If a business's site
    names zero people anywhere (no team page, no about-page bios) this has
    nothing to derive a pattern from and falls back to generic guesses only.
  - SMTP verification requires outbound port 25, which MANY home/office
    networks and ISPs block by default. When blocked, every probe returns
    "unverified" (not "invalid" -- never treated as a false negative) and this
    prints one clear warning at batch start rather than failing silently.
  - Many mail servers (esp. Microsoft 365, Google Workspace) don't give a
    truthful RCPT response at all -- they accept everything at SMTP time and
    bounce later. Those show up as "unverified" or falsely "valid". Treat any
    address here as a *candidate*, not a guarantee -- Stage 6 QA + a
    conservative send volume (Stage 7) are what actually cap bounce damage.
  - Pattern brute-force is capped (a handful of decision-maker-titled people,
    a handful of templates each) on purpose -- unbounded brute-forcing would
    mean dozens of SMTP probes per domain, slow and rude to the target's mail
    server across an 850-domain run. It will not find every possible person's
    email; it targets the people worth targeting for a sales pitch.
  - The name/title regex match (the prose-text supplementary path) is a
    heuristic, not NLP. It will miss names written oddly and occasionally
    mismatch. The structural team-page extraction (primary path, from the
    crawler) is far more reliable since it reads actual name/title DOM pairs
    rather than guessing from sentences.

Usage:
    python contacts.py --crawl-dir ../crawler/output
    python contacts.py --crawl-dir ../crawler/output --verify-smtp
    python contacts.py --crawl-dir ../crawler/output --domain stripe.com --verify-smtp
    python contacts.py --crawl-dir ../crawler/output --domains-file leads.csv --domain-column website

Output: <crawl-dir>/<domain>/contacts.json
    {
      "domain": "example.com",
      "processed_at": "...",
      "status": "ok" | "no_crawl_data" | "no_contacts_found",
      "contacts": [
        {
          "email": "jane@example.com",
          "role": "founder_owner",
          "matched_name": "Jane Diaz",
          "matched_title": "Founder",
          "source": "scraped" | "pattern_derived" | "pattern_bruteforce_verified" | "pattern_guessed" | "guessed",
          "verification": "valid" | "catch_all" | "unverified" | "not_checked",
          "verification_note": "...",
          "confidence": "high" | "medium" | "low",
          "linkedin_lookup_url": "https://www.google.com/search?q=..."
        }
      ],
      "smtp_probing_enabled": true
    }
"""

import argparse
import csv
import json
import logging
import logging.handlers
import os
import random
import re
import smtplib
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import dns.resolver
import requests
from bs4 import BeautifulSoup

log = logging.getLogger("contacts")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

MAX_CONTACTS_PER_DOMAIN = 2
SMTP_TIMEOUT_SEC = 8
MX_LOOKUP_TIMEOUT_SEC = 5
HELO_DOMAIN = "coldemailleadcrawler.local"
COMMON_GUESS_PREFIXES = ["info", "contact", "sales", "hello"]

# How many named-but-emailless people we'll spend SMTP probes trying to
# resolve per domain, and how many pattern templates we'll try per person.
# Bounded on purpose: unbounded brute force means dozens of probes per domain,
# 850 domains in a batch -- slow, and rude to target mail servers.
MAX_BRUTEFORCE_CANDIDATES = 5
MAX_BRUTEFORCE_TEMPLATES = 5

# Titles worth spending a probe budget on for a B2B sales pitch. Deliberately
# excludes generic staff titles (Engineer, Advocate, Analyst, Coordinator) --
# those are real people (the crawler finds them fine) but not who a cold sales
# email should target, so no point burning probes resolving their email.
DECISION_MAKER_TITLE_RE = re.compile(
    r"\b(Co-?Founder|Founder|Chief [A-Za-z]+ Officer|CEO|CTO|CFO|COO|President|Owner|"
    r"Managing Director|Director|Head of [A-Za-z &]+|VP of [A-Za-z &]+|Vice President|"
    r"General Manager|Business Development|Marketing Manager|Sales Manager)\b",
    re.I,
)
# used to sort decision-makers so limited probe budget goes to the most senior
# person first (founder/CEO beats "Director of Product Engineering")
SENIORITY_RANK_RE = [
    (1, re.compile(r"\b(Co-?Founder|Founder|CEO|Chief Executive Officer|President|Owner)\b", re.I)),
    (2, re.compile(r"\b(CTO|CFO|COO|Chief [A-Za-z]+ Officer|Managing Director|VP of [A-Za-z &]+|Vice President)\b", re.I)),
    (3, re.compile(r"\b(Sales Manager|Marketing Manager|Business Development|Head of [A-Za-z &]+)\b", re.I)),
]

# Ordered roughly by real-world prevalence in public email-pattern studies.
# {f}=first name, {l}=last name, {fi}=first initial, {li}=last initial
PATTERN_TEMPLATES = [
    "{f}.{l}",
    "{f}{l}",
    "{fi}{l}",
    "{f}",
    "{f}_{l}",
    "{l}.{f}",
    "{f}.{li}",
    "{fi}.{l}",
]

# role -> (priority rank, regex on the local part before @). Lower rank = better
# contact for a sales pitch. Order in this list also matters for classify_email.
ROLE_PATTERNS = [
    ("founder_owner", 1, re.compile(r"^(founder|co-?founder|owner|ceo|president|principal)\b", re.I)),
    ("sales_bd", 1, re.compile(r"^(sales|bd|biz\.?dev|business\.?development|partnerships?)\b", re.I)),
    ("marketing", 2, re.compile(r"^(marketing|growth|brand)\b", re.I)),
    ("personal", 2, re.compile(r"^[a-z]+[._][a-z]+$", re.I)),  # firstname.lastname / firstname_lastname
    ("generic_contact", 3, re.compile(r"^(info|contact|hello|hi|team|office|admin|enquiries?)\b", re.I)),
    ("support", 4, re.compile(r"^(support|help|service|customerservice)\b", re.I)),
    ("hr_careers", 5, re.compile(r"^(hr|careers?|jobs|recruit(ing)?)\b", re.I)),
]
DEFAULT_ROLE = ("other", 4)

TITLE_WORDS = (
    r"Co-?Founder|Founder|Chief Executive Officer|CEO|President|Owner|"
    r"Managing Director|Director|Head of Sales|Sales Manager|VP of Sales|"
    r"Business Development|Marketing Manager|General Manager"
)
NAME_RE = r"[A-Z][a-z]+(?:\s[A-Z]\.?)?\s[A-Z][a-z]+"
# "Jane Diaz, Founder" / "Jane Diaz - CEO" / "Jane Diaz is our Founder"
NAME_THEN_TITLE = re.compile(rf"({NAME_RE})\s*[,\-–—]?\s*(?:is\s+(?:the|our)\s+)?({TITLE_WORDS})")
# "Founder: Jane Diaz" / "CEO, Jane Diaz"
TITLE_THEN_NAME = re.compile(rf"({TITLE_WORDS})\s*[,:\-–—]\s*({NAME_RE})")
# "I'm Jason Fried, one of the co-founders here." / "I am Jane Diaz, the Founder
# of Acme." -- verified live on basecamp.com: this exact first-person
# self-introduction phrasing (extremely common on solo-founder/small-business
# About pages) was invisible to both patterns above, which only match
# third-person "Name, Title" shapes. `s?` allows the plural ("co-founders")
# people use when describing themselves as one of a group of founders.
# real website prose overwhelmingly uses a typographic/curly apostrophe
# (U+2019, "'") rather than a straight ASCII one -- verified live on
# basecamp.com's actual about page text ("I’m Jason Fried..."). Matching
# only the ASCII apostrophe silently missed exactly this common case.
SELF_INTRO_RE = re.compile(
    rf"\b(?:I[’'`]?m|I am|My name is)\s+({NAME_RE})\s*,?\s*"
    rf"(?:one of the\s+|the\s+|an?\s+)?({TITLE_WORDS})s?\b",
    re.I,
)
# Verified live on stripe.com's careers page: a book-endorsement blurb reads
# "Aaron Levie / CEO at Box" -- NAME_THEN_TITLE correctly extracts "Aaron
# Levie, CEO" but that's Box's CEO, not Stripe's. This catches the trailing
# "at/of/@ Company" qualifier the title match itself doesn't capture, so it
# can be checked against the domain being crawled and rejected if it names
# someone else's company.
TRAILING_COMPANY_RE = re.compile(r"^\s*(?:at|of|@|,)\s+([A-Z][A-Za-z0-9&.\-]+(?:\s[A-Z][A-Za-z0-9&.\-]+){0,2})")


def _company_guess_from_domain(domain: str) -> str:
    """'stripe.com' -> 'stripe', 'buffer.com' -> 'buffer' -- crude but enough
    to tell "this company" apart from an unrelated one named in a testimonial."""
    return re.sub(r"[^a-z0-9]", "", domain.split(".")[0].lower())


def _mentions_other_company(remaining_text: str, own_company: str) -> bool:
    m = TRAILING_COMPANY_RE.match(remaining_text)
    if not m:
        return False
    mentioned = re.sub(r"[^a-zA-Z0-9]", "", m.group(1)).lower()
    if not mentioned or not own_company:
        return False
    return own_company not in mentioned and mentioned not in own_company


def setup_logging(out_dir: Path):
    log.setLevel(logging.INFO)
    log.handlers.clear()
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    log.addHandler(console)
    log_dir = out_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    fh = logging.handlers.RotatingFileHandler(log_dir / "contacts.log", maxBytes=5_000_000,
                                               backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)


# ---------------------------------------------------------------------------
# Role classification + name matching
# ---------------------------------------------------------------------------

def classify_email(email: str) -> tuple[str, int]:
    local = email.split("@", 1)[0]
    for role, priority, pattern in ROLE_PATTERNS:
        if pattern.search(local):
            return role, priority
    return DEFAULT_ROLE


def extract_named_people(crawl_result: dict, opencorporates_token: str = None) -> list:
    """Primary source: the crawler's structural team-page extraction
    (`team_members` -- name+title pairs read directly from DOM structure,
    e.g. <h3><button>Jane Diaz</button></h3> next to <p>Founder</p>). This is
    far more reliable than prose regex since it doesn't depend on someone
    having written a sentence like "Jane Diaz, Founder" anywhere.

    Supplementary source: a regex scan of about/contact/careers prose text
    for the same "Name, Title" shape, for sites that mention a founder in a
    paragraph rather than a team-page card. Heuristic, not NLP -- see module
    LIMITATIONS.

    Last-resort source, opt-in: when the site names literally nobody AND an
    OpenCorporates API token is supplied, look up government-filed officer
    records for the company -- the actual bottleneck this tool has vs.
    Apollo is exactly this case (a site that publishes no names at all).

    Returns list of {name, title}, deduped by name."""
    people = []
    seen = set()
    own_company = _company_guess_from_domain(crawl_result.get("domain", ""))

    for person in crawl_result.get("team_members", []):
        key = person["name"].lower()
        if key not in seen:
            seen.add(key)
            people.append({"name": person["name"], "title": person.get("title", "")})

    for page in crawl_result.get("pages", []):
        if page.get("page_type") not in ("about", "team", "contact", "careers", "home"):
            continue
        text = page.get("text", "")
        for pattern in (NAME_THEN_TITLE, TITLE_THEN_NAME, SELF_INTRO_RE):
            for match in pattern.finditer(text):
                groups = match.groups()
                name, title = (groups[1], groups[0]) if pattern is TITLE_THEN_NAME else (groups[0], groups[1])
                # verified live on stripe.com: a careers-page book blurb reads
                # "Aaron Levie / CEO at Box" -- reject when the text right
                # after the match names a DIFFERENT company (Box, not Stripe)
                if _mentions_other_company(text[match.end():match.end() + 40], own_company):
                    continue
                key = name.lower()
                if key not in seen:
                    seen.add(key)
                    people.append({"name": name.strip(), "title": title.strip()})

    if not people and opencorporates_token:
        company_guess = own_company.capitalize() if own_company else crawl_result.get("domain", "")
        for person in opencorporates_lookup(company_guess, api_token=opencorporates_token):
            key = person["name"].lower()
            if key not in seen:
                seen.add(key)
                people.append(person)

    return people


def match_email_to_person(email: str, people: list) -> dict | None:
    """If the email local-part plausibly encodes a name we found on-site,
    return that person (bumps confidence significantly)."""
    local = re.sub(r"[._]", " ", email.split("@", 1)[0]).lower()
    local_tokens = set(local.split())
    for person in people:
        name_tokens = set(person["name"].lower().split())
        if name_tokens and name_tokens.issubset(local_tokens):
            return person
        # also handle firstname-only local parts (jane@ -> "Jane Diaz")
        first = person["name"].split()[0].lower()
        if local == first:
            return person
    return None


def linkedin_lookup_url(name: str, domain: str) -> str:
    """NOT a scraper -- just builds a search URL for a human to glance at.
    See module docstring for why automated LinkedIn scraping isn't attempted."""
    from urllib.parse import quote_plus
    query = quote_plus(f'"{name}" {domain} site:linkedin.com/in')
    return f"https://www.google.com/search?q={query}"


SEARCH_FALLBACK_MAX_RESULTS = 5
SEARCH_FALLBACK_MAX_PEOPLE = 2
SEARCH_FALLBACK_TITLE_TERMS = "CEO OR founder OR president OR owner"


def search_engine_fallback_people(company_name: str, domain: str) -> list:
    """NOT wired into the batch pipeline (process_domain / build_candidates)
    -- call this manually, one domain at a time, if at all. Verified live
    while building this: DuckDuckGo's HTML endpoint actively CAPTCHA-walls
    automated requests, and Bing -- which initially returned clean results --
    degraded to serving IRRELEVANT results (SpongeBob's Patrick Star for a
    query about Stripe's Patrick Collison) after only a handful of automated
    queries in the same session. Neither search engine reliably tolerates
    the repeated automated querying this tool's actual use case needs (many
    domains in a batch). Function kept for occasional manual use on one
    specific hard lead, NOT as a scaled Apollo-parity closer -- that claim
    would be false advertising after what live testing actually showed.

    Extends coverage beyond what the crawled site itself publishes, for a
    domain that names nobody at all (the actual bottleneck behind the
    Apollo-parity gap: this tool otherwise only ever sees one company's own
    site).

    NOT platform scraping: reads a public search engine's own results page,
    same content a person manually searching would see -- not a protected
    platform's private data. Still gray-area (most search engines' terms
    discourage automated querying), so this stays opt-in, capped to ONE
    HTTP request, a handful of result snippets, and reuses the exact same
    third-party-company rejection check as on-site extraction (search
    snippets are, if anything, MORE likely to surface an unrelated person)."""
    own_company = _company_guess_from_domain(domain)
    query = f'"{company_name}" {SEARCH_FALLBACK_TITLE_TERMS}'

    try:
        r = requests.get(
            "https://www.bing.com/search",
            params={"q": query, "setlang": "en", "mkt": "en-US"},
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"},
            timeout=8,
        )
        if r.status_code != 200:
            return []
        soup = BeautifulSoup(r.text, "lxml")
        snippets = [el.get_text(" ") for el in soup.select("li.b_algo .b_caption p")][:SEARCH_FALLBACK_MAX_RESULTS]
    except Exception:
        return []

    people = []
    seen = set()
    for text in snippets:
        for pattern in (NAME_THEN_TITLE, TITLE_THEN_NAME, SELF_INTRO_RE):
            for match in pattern.finditer(text):
                groups = match.groups()
                name, title = (groups[1], groups[0]) if pattern is TITLE_THEN_NAME else (groups[0], groups[1])
                if _mentions_other_company(text[match.end():match.end() + 40], own_company):
                    continue
                key = name.lower()
                if key in seen:
                    continue
                seen.add(key)
                people.append({"name": name.strip(), "title": title.strip(), "source": "search_fallback"})
                if len(people) >= SEARCH_FALLBACK_MAX_PEOPLE:
                    return people
    return people


OPENCORPORATES_TIMEOUT_SEC = 10


def opencorporates_lookup(company_name: str, api_token: str = None) -> list:
    """Government-filed officer/director records via OpenCorporates' public
    registry API (aggregates real filings across ~140 jurisdictions) --
    finds the company, then reads its officer list. Requires the caller's
    own free-tier API token (register at opencorporates.com/api_accounts/new
    -- this tool cannot create accounts on your behalf). A real sanctioned
    API relationship, not scraping-detection cat-and-mouse like a search
    engine, so this is structurally more durable for batch use than
    search_engine_fallback_people -- but NOT live-verified against real data
    this session, since no token was available. Implemented per
    OpenCorporates' documented v0.4 response schema."""
    if not api_token:
        return []
    try:
        search_resp = requests.get(
            "https://api.opencorporates.com/v0.4/companies/search",
            params={"q": company_name, "api_token": api_token},
            timeout=OPENCORPORATES_TIMEOUT_SEC,
        )
        if search_resp.status_code != 200:
            return []
        companies = search_resp.json().get("results", {}).get("companies", [])
        if not companies:
            return []
        best = companies[0].get("company", {})
        jurisdiction = best.get("jurisdiction_code")
        number = best.get("company_number")
        if not jurisdiction or not number:
            return []

        detail_resp = requests.get(
            f"https://api.opencorporates.com/v0.4/companies/{jurisdiction}/{number}",
            params={"api_token": api_token},
            timeout=OPENCORPORATES_TIMEOUT_SEC,
        )
        if detail_resp.status_code != 200:
            return []
        officers = detail_resp.json().get("results", {}).get("company", {}).get("officers", [])

        people = []
        seen = set()
        for entry in officers:
            officer = entry.get("officer", {})
            name = (officer.get("name") or "").strip()
            if not name or name.lower() in seen:
                continue
            seen.add(name.lower())
            title = officer.get("position") or "Officer"
            people.append({"name": name, "title": title, "source": "opencorporates"})
        return people
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Email pattern inference -- Apollo's actual core trick, done for free
# ---------------------------------------------------------------------------
#
# Apollo's "email finder" mostly just applies a known convention for a domain
# ({first}.{last}@, {f}{last}@, etc.) that it learned from other examples at
# that company. We can derive the same convention ourselves the moment we have
# ONE confirmed real (name, email) pair at a domain, then apply it to every
# other named person who has no directly-published email.

def _name_parts(name: str) -> tuple | None:
    parts = [p for p in re.split(r"\s+", name.strip()) if p]
    if len(parts) < 2:
        return None
    clean = lambda s: re.sub(r"[^a-zA-Z]", "", s).lower()
    first, last = clean(parts[0]), clean(parts[-1])
    if not first or not last:
        return None
    return first, last


def apply_pattern(template: str, name: str) -> str | None:
    """template uses {f}/{l}/{fi}/{li} placeholders. Returns a local-part
    string, or None if `name` doesn't have a usable first+last shape."""
    parts = _name_parts(name)
    if not parts:
        return None
    f, l = parts
    return template.format(f=f, l=l, fi=f[0], li=l[0])


def detect_pattern(confirmed_pairs: list) -> str | None:
    """confirmed_pairs: [(name, email)] where the email is already known (via
    match_email_to_person) to belong to that name. Returns the template that
    explains the pairs, or None if nothing fits (nicknames, numbered emails,
    initials-only addresses, etc. -- common enough that this must degrade
    gracefully rather than derive a wrong pattern)."""
    votes: dict = {}
    for name, email in confirmed_pairs:
        local = email.split("@", 1)[0].lower()
        for template in PATTERN_TEMPLATES:
            if apply_pattern(template, name) == local:
                votes[template] = votes.get(template, 0) + 1
                break  # templates are ordered by likelihood -- first hit wins for this pair
    if not votes:
        return None
    return max(votes.items(), key=lambda kv: kv[1])[0]


def seniority_rank(title: str) -> int:
    for rank, pattern in SENIORITY_RANK_RE:
        if pattern.search(title or ""):
            return rank
    return 9


# ---------------------------------------------------------------------------
# Verification (MX + best-effort SMTP)
# ---------------------------------------------------------------------------

def get_mx_hosts(domain: str) -> list:
    try:
        answers = dns.resolver.resolve(domain, "MX", lifetime=MX_LOOKUP_TIMEOUT_SEC)
        ranked = sorted((r.preference, str(r.exchange).rstrip(".")) for r in answers)
        return [host for _, host in ranked]
    except Exception:
        return []


def smtp_probe(email: str, mx_hosts: list) -> tuple[str, str]:
    """One RCPT TO attempt. Returns (status, note). status in:
    'valid', 'invalid', 'unverified'. Never raises."""
    if not mx_hosts:
        return "unverified", "no MX records found for this domain"
    last_note = "all MX hosts unreachable"
    for host in mx_hosts[:2]:
        try:
            with smtplib.SMTP(timeout=SMTP_TIMEOUT_SEC) as smtp:
                smtp.connect(host, 25)
                smtp.helo(HELO_DOMAIN)
                smtp.mail(f"verify@{HELO_DOMAIN}")
                code, message = smtp.rcpt(email)
                msg = message.decode(errors="replace") if isinstance(message, bytes) else str(message)
                if code == 250:
                    return "valid", f"RCPT 250 from {host}"
                if code in (550, 551, 553, 554):
                    return "invalid", f"RCPT {code} from {host}: {msg[:120]}"
                last_note = f"ambiguous RCPT {code} from {host}"
        except (socket.timeout, ConnectionRefusedError, OSError) as e:
            last_note = f"connection to {host} failed: {e} (port 25 outbound may be blocked on this network)"
            continue
        except smtplib.SMTPException as e:
            last_note = f"SMTP error from {host}: {e}"
            continue
    return "unverified", last_note


def check_catch_all(domain: str, mx_hosts: list) -> bool:
    """Probe one throwaway address once to learn if a domain accepts ANY
    address. Computed once per domain and cached by the caller -- both to
    save probes (politeness to the target mail server) and because brute-force
    pattern guessing is meaningless on a catch-all domain (every guess would
    come back 'valid' whether or not it's real)."""
    if not mx_hosts:
        return False
    fake_local = f"nonexistent-probe-{random.randint(100000, 999999)}"
    status, _ = smtp_probe(f"{fake_local}@{domain}", mx_hosts)
    return status == "valid"


def verify_email(email: str, domain: str, mx_hosts: list, known_catch_all: bool = None) -> tuple[str, str]:
    """RCPT probe + catch-all handling. known_catch_all, when already computed
    by the caller (see check_catch_all), skips a redundant probe -- a domain
    known to be catch-all always reports catch_all without probing at all;
    a domain known NOT to be catch-all skips the extra fake-address probe."""
    if known_catch_all is True:
        return "catch_all", "domain is catch-all -- cannot confirm this specific mailbox exists"
    status, note = smtp_probe(email, mx_hosts)
    if status != "valid":
        return status, note
    if known_catch_all is False:
        return "valid", note
    # known_catch_all is None (caller didn't precompute it) -- fall back to a
    # one-off double-probe, same as before this function took the parameter
    fake_local = f"nonexistent-probe-{random.randint(100000, 999999)}"
    fake_status, _ = smtp_probe(f"{fake_local}@{domain}", mx_hosts)
    if fake_status == "valid":
        return "catch_all", "domain accepts any address -- cannot confirm this specific mailbox, treat as lower-confidence"
    return "valid", note


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

def build_candidates(crawl_result: dict, opencorporates_token: str = None) -> tuple[list, list]:
    """Returns (candidates, pending_people). `pending_people` are named
    individuals with no scraped email AND no domain-wide pattern to derive one
    from -- process_domain resolves those via bounded brute-force/guessing,
    since that step needs network access (SMTP) this pure function doesn't have."""
    domain = crawl_result["domain"]
    emails = crawl_result.get("emails", [])
    people = extract_named_people(crawl_result, opencorporates_token=opencorporates_token)

    candidates = []
    confirmed_pairs = []
    resolved_names = set()

    for email in emails:
        role, priority = classify_email(email)
        person = match_email_to_person(email, people)
        if person:
            priority -= 1  # matched-to-a-real-name beats an unmatched same-role address
            confirmed_pairs.append((person["name"], email))
            resolved_names.add(person["name"].lower())
        candidates.append({
            "email": email,
            "role": role,
            "priority": priority,
            "matched_name": person["name"] if person else None,
            "matched_title": person["title"] if person else None,
            "source": "scraped",
        })

    # Apollo's core trick, for free: a confirmed (name, email) pair reveals the
    # domain's email convention -- apply it to every other named person we
    # found who has no directly-published email of their own.
    pattern = detect_pattern(confirmed_pairs)
    existing_emails = {c["email"] for c in candidates}
    pending_people = []
    for person in people:
        if person["name"].lower() in resolved_names:
            continue
        if pattern:
            local = apply_pattern(pattern, person["name"])
            if local:
                candidate_email = f"{local}@{domain}"
                if candidate_email not in existing_emails:
                    role, priority = classify_email(candidate_email)
                    candidates.append({
                        "email": candidate_email,
                        "role": role,
                        "priority": priority - 1,  # named + derived from real evidence at this domain
                        "matched_name": person["name"],
                        "matched_title": person.get("title"),
                        "source": "pattern_derived",
                    })
                    existing_emails.add(candidate_email)
                continue
        pending_people.append(person)

    if not emails:
        for prefix in COMMON_GUESS_PREFIXES:
            role, priority = classify_email(f"{prefix}@{domain}")
            candidates.append({
                "email": f"{prefix}@{domain}",
                "role": role,
                "priority": priority + 3,  # guesses always rank below anything actually found
                "matched_name": None,
                "matched_title": None,
                "source": "guessed",
            })

    candidates.sort(key=lambda c: (c["priority"], c["source"] == "guessed", c["email"]))
    return candidates, pending_people


def resolve_pattern_candidates(pending_people: list, domain: str, mx_hosts: list,
                                verify_smtp: bool, catch_all: bool) -> list:
    """For named decision-makers with no scraped email and no derivable domain
    pattern: either brute-force a small set of common conventions and keep only
    an SMTP-confirmed one, or (if we can't verify) emit a single clearly-labeled
    speculative guess. Bounded to MAX_BRUTEFORCE_CANDIDATES people, sorted by
    seniority, so a domain with 30 named staff doesn't turn into 150+ probes."""
    decision_makers = [p for p in pending_people if DECISION_MAKER_TITLE_RE.search(p.get("title") or "")]
    decision_makers.sort(key=lambda p: seniority_rank(p.get("title") or ""))
    decision_makers = decision_makers[:MAX_BRUTEFORCE_CANDIDATES]

    resolved = []
    for person in decision_makers:
        role_default, _ = classify_email(f"placeholder@{domain}")
        if verify_smtp and mx_hosts and not catch_all:
            found = None
            for template in PATTERN_TEMPLATES[:MAX_BRUTEFORCE_TEMPLATES]:
                local = apply_pattern(template, person["name"])
                if not local:
                    continue
                candidate_email = f"{local}@{domain}"
                status, note = smtp_probe(candidate_email, mx_hosts)
                time.sleep(0.3)  # polite delay between probes against the same server
                if status == "valid":
                    found = (candidate_email, note)
                    break
            if found:
                role, _ = classify_email(found[0])
                resolved.append({
                    "email": found[0], "role": role,
                    "matched_name": person["name"], "matched_title": person.get("title"),
                    "source": "pattern_bruteforce_verified",
                    "verification": "valid", "verification_note": found[1],
                })
            # else: brute force found nothing real for this person -- say
            # nothing rather than guess wrong (no unverified entry added)
        else:
            local = apply_pattern(PATTERN_TEMPLATES[0], person["name"])
            if not local:
                continue
            candidate_email = f"{local}@{domain}"
            role, _ = classify_email(candidate_email)
            if catch_all:
                note = "domain is catch-all -- cannot verify any specific guess"
            elif not mx_hosts:
                note = "no MX records -- cannot verify"
            else:
                note = "no confirmed email pattern for this domain -- speculative guess using the most common convention (--verify-smtp not enabled)"
            resolved.append({
                "email": candidate_email, "role": role,
                "matched_name": person["name"], "matched_title": person.get("title"),
                "source": "pattern_guessed",
                "verification": "catch_all" if catch_all else "unverified",
                "verification_note": note,
            })
    return resolved


def confidence_for(candidate: dict, verification: str) -> str:
    """Verified live (basecamp.com): a real scraped address matched to the
    actual co-founder's name got dumped into 'low' just because SMTP came
    back ambiguous (450, greylisted -- extremely common, see module
    LIMITATIONS) -- the old rule treated ANY non-'valid' verification the
    same regardless of whether a real name backed the address. A found name
    is real signal on its own; it shouldn't be thrown away because SMTP
    verification (independently known to be unreliable) was inconclusive."""
    if verification == "invalid":
        return "reject"  # never surfaced -- filtered out before output
    if candidate["source"] in ("guessed", "pattern_guessed"):
        return "low"  # a blind guess is a blind guess regardless of anything else
    if verification == "valid":
        return "high" if candidate["matched_name"] else "medium"
    if verification == "not_checked":
        return "medium"  # operator chose not to verify -- no negative signal either
    # verification is "unverified" or "catch_all" -- SMTP was attempted but
    # inconclusive. A name match still counts for something; bare unnamed
    # addresses in this state are the weakest tier.
    return "medium" if candidate["matched_name"] else "low"


def process_domain(domain_dir: Path, verify_smtp: bool, opencorporates_token: str = None) -> dict:
    domain = domain_dir.name
    crawl_path = domain_dir / "crawl_result.json"
    result = {
        "domain": domain,
        "processed_at": datetime.now(timezone.utc).isoformat(),
        "status": "no_crawl_data",
        "contacts": [],
        "smtp_probing_enabled": verify_smtp,
    }

    if not crawl_path.exists():
        log.warning(f"[{domain}] no crawl_result.json found, skipping")
        return result

    crawl_result = json.loads(crawl_path.read_text(encoding="utf-8"))
    if crawl_result.get("status") != "ok":
        result["status"] = "no_crawl_data"
        result["error"] = f"upstream crawl status was '{crawl_result.get('status')}', not 'ok'"
        return result

    candidates, pending_people = build_candidates(crawl_result, opencorporates_token=opencorporates_token)

    # Verify against each candidate's OWN email domain, not the crawled site's
    # domain -- a scraped address can legitimately live on a different domain
    # (e.g. site is notion.so, published contact is team@makenotion.com). Using
    # the wrong domain for MX/SMTP would falsely report "no MX" on good addresses.
    mx_cache: dict = {}
    catch_all_cache: dict = {}

    def mx_for(email_domain: str) -> list:
        if email_domain not in mx_cache:
            mx_cache[email_domain] = get_mx_hosts(email_domain)
            if not mx_cache[email_domain]:
                log.warning(f"[{domain}] no MX records found for {email_domain} "
                            f"-- that mailbox domain likely can't receive email")
        return mx_cache[email_domain]

    def catch_all_for(email_domain: str, mx_hosts: list) -> bool:
        if not verify_smtp or not mx_hosts:
            return False
        if email_domain not in catch_all_cache:
            catch_all_cache[email_domain] = check_catch_all(email_domain, mx_hosts)
            time.sleep(0.3)
        return catch_all_cache[email_domain]

    # Apollo's "find anyone at this company" -- for named people with no
    # scraped email and no derivable domain pattern, spend a bounded SMTP
    # budget trying to resolve one. Always targets the crawled site's own
    # domain (a generated guess has nowhere else to live).
    if pending_people:
        domain_mx = mx_for(domain)
        domain_catch_all = catch_all_for(domain, domain_mx)
        resolved = resolve_pattern_candidates(pending_people, domain, domain_mx, verify_smtp, domain_catch_all)
        for r in resolved:
            r["priority"] = 0 if r["source"] == "pattern_bruteforce_verified" else 3
        candidates.extend(resolved)
        candidates.sort(key=lambda c: (c["priority"], c["source"] in ("guessed", "pattern_guessed"), c["email"]))

    kept = []
    for c in candidates:
        if len(kept) >= MAX_CONTACTS_PER_DOMAIN:
            break
        email_domain = c["email"].rsplit("@", 1)[-1]
        mx_hosts = mx_for(email_domain)
        if "verification" in c:
            # already resolved above (pattern brute-force / guess) -- don't re-probe
            verification, note = c["verification"], c["verification_note"]
        elif not mx_hosts:
            verification, note = "unverified", "no MX records"
        elif verify_smtp:
            known_ca = catch_all_for(email_domain, mx_hosts)
            verification, note = verify_email(c["email"], email_domain, mx_hosts, known_catch_all=known_ca)
            time.sleep(0.3)  # be polite to the target mail server between probes
        else:
            verification, note = "not_checked", "SMTP verification disabled (--verify-smtp not passed)"

        if verification == "invalid":
            log.info(f"[{domain}] dropping {c['email']} -- verified invalid ({note})")
            continue

        c["verification"] = verification
        c["verification_note"] = note
        c["confidence"] = confidence_for(c, verification)
        c["linkedin_lookup_url"] = linkedin_lookup_url(c["matched_name"], domain) if c.get("matched_name") else None
        del c["priority"]
        kept.append(c)

    result["contacts"] = kept
    result["status"] = "ok" if kept else "no_contacts_found"
    log.info(f"[{domain}] status={result['status']} contacts={len(kept)} "
             f"({', '.join(c['email'] + ':' + c['confidence'] for c in kept)})")
    return result


# ---------------------------------------------------------------------------
# Batch runner
# ---------------------------------------------------------------------------

def load_domain_filter(path: str, column: str) -> set:
    domains = set()
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if column not in reader.fieldnames:
            raise SystemExit(f"Column '{column}' not found. Available columns: {reader.fieldnames}")
        for row in reader:
            val = (row.get(column) or "").strip()
            if val:
                domains.add(val)
    return domains


def write_summary(crawl_dir: Path, all_results: list):
    path = crawl_dir / "_contacts_summary.csv"
    fields = ["domain", "status", "contacts_found", "best_email", "best_confidence", "best_verification"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in all_results:
            best = r["contacts"][0] if r["contacts"] else {}
            w.writerow({
                "domain": r["domain"],
                "status": r["status"],
                "contacts_found": len(r["contacts"]),
                "best_email": best.get("email", ""),
                "best_confidence": best.get("confidence", ""),
                "best_verification": best.get("verification", ""),
            })
    log.info(f"Contacts summary written: {path}")


def run_batch(crawl_dir: Path, verify_smtp: bool, domain_filter: set = None, opencorporates_token: str = None):
    domain_dirs = sorted(d for d in crawl_dir.iterdir() if d.is_dir() and (d / "crawl_result.json").exists())
    if domain_filter:
        domain_dirs = [d for d in domain_dirs if d.name in domain_filter]

    if not domain_dirs:
        log.error(f"no crawl_result.json files found under {crawl_dir}")
        return

    if verify_smtp:
        log.info("SMTP verification ENABLED -- probes outbound port 25 per candidate. "
                 "If your network/ISP blocks port 25, every probe will return 'unverified' "
                 "(never falsely 'invalid') -- check the log for repeated connection-failed notes.")
    else:
        log.info("SMTP verification disabled (pass --verify-smtp to enable). "
                 "Contacts will carry verification='not_checked' -- MX presence is still checked.")

    if opencorporates_token:
        log.info("OpenCorporates fallback ENABLED -- used only for domains whose site names nobody at all.")

    log.info(f"Processing {len(domain_dirs)} domain(s) from {crawl_dir}")
    all_results = []
    for i, d in enumerate(domain_dirs, 1):
        r = process_domain(d, verify_smtp, opencorporates_token=opencorporates_token)
        (d / "contacts.json").write_text(json.dumps(r, indent=2, ensure_ascii=False), encoding="utf-8")
        all_results.append(r)
        if i % 10 == 0 or i == len(domain_dirs):
            log.info(f"[progress] {i}/{len(domain_dirs)} domains processed")

    write_summary(crawl_dir, all_results)
    ok = sum(1 for r in all_results if r["status"] == "ok")
    none_found = sum(1 for r in all_results if r["status"] == "no_contacts_found")
    no_data = sum(1 for r in all_results if r["status"] == "no_crawl_data")
    log.info(f"Done. contacts_found={ok} no_contacts={none_found} no_crawl_data={no_data}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    global MAX_CONTACTS_PER_DOMAIN

    ap = argparse.ArgumentParser(description="Free contact discovery + verification (Stage 4).")
    ap.add_argument("--crawl-dir", default="../crawler/output",
                     help="output dir from crawl.py, containing <domain>/crawl_result.json (default: ../crawler/output)")
    ap.add_argument("--domain", help="process a single domain only")
    ap.add_argument("--domains-file", help="CSV restricting processing to these domains")
    ap.add_argument("--domain-column", default="website", help="column name in --domains-file")
    ap.add_argument("--verify-smtp", action="store_true",
                     help="enable best-effort SMTP RCPT verification (needs outbound port 25; "
                          "see module docstring LIMITATIONS)")
    ap.add_argument("--max-contacts", type=int, default=MAX_CONTACTS_PER_DOMAIN,
                     help="max contacts kept per domain (default 2)")
    ap.add_argument("--opencorporates-token", default=os.environ.get("OPENCORPORATES_API_TOKEN"),
                     help="OpenCorporates API token (free, register at opencorporates.com/api_accounts/new). "
                          "Also settable via OPENCORPORATES_API_TOKEN env var. Used only as a last resort, "
                          "for domains whose site names nobody at all.")
    args = ap.parse_args()
    MAX_CONTACTS_PER_DOMAIN = args.max_contacts

    crawl_dir = Path(args.crawl_dir)
    if not crawl_dir.exists():
        raise SystemExit(f"--crawl-dir not found: {crawl_dir}")

    setup_logging(crawl_dir)

    domain_filter = None
    if args.domain:
        domain_filter = {args.domain}
    elif args.domains_file:
        domain_filter = load_domain_filter(args.domains_file, args.domain_column)

    run_batch(crawl_dir, args.verify_smtp, domain_filter, opencorporates_token=args.opencorporates_token)


if __name__ == "__main__":
    main()
