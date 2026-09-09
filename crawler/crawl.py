"""
Free self-hosted replacement for Firecrawl.

Does what Firecrawl does for this pipeline:
  - Renders JS pages (Playwright headless Chromium)
  - Dismisses cookie/consent popups automatically
  - Extracts clean article-style text (trafilatura, strips nav/footer/ads)
  - Discovers the pages that matter (about, contact, services/pricing, blog x2, careers)
  - Pulls mailto: emails found on-site (bonus: partial Apollo replacement)
  - Never crashes the whole run on one bad site -- logs and moves on
  - Never fabricates content on failure -- distinguishes crawl_failed / blocked / robots_disallowed
  - Caches results so a re-run doesn't re-hit sites already crawled recently

Hardening beyond a minimal scraper (why this can replace Firecrawl for this job):
  - Basic stealth (webdriver flag hidden, realistic UA rotation, real Accept-Language)
    -- default Playwright Chromium is trivially fingerprinted and degraded/blocked by
       many sites otherwise.
  - Tolerates bad/self-signed TLS certs -- very common on small local-business sites,
    which is most of a cold-email lead list. Firecrawl handles this; a naive scraper
    hard-fails and silently loses a chunk of your leads.
  - Distinguishes permanent errors (DNS not found) from transient ones (timeout,
    connection reset) and only retries the transient kind, with exponential backoff.
  - Detects bot-block / CAPTCHA walls (Cloudflare "Just a moment", "Access Denied",
    etc.) and reports status=blocked distinctly from a genuine crawl_failed, so the
    pipeline can route it differently (e.g. skip retry, flag for manual lookup)
    instead of wasting retries against a wall that will never pass.
  - Respects robots.txt by default (ethical baseline + reduces block-triggering).
  - Sitemap.xml fallback discovery, in addition to nav-link scanning, for sites whose
    nav is hidden behind a hamburger menu or client-side router.
  - Flags low_content pages (likely a JS wall or paywall that "succeeded" but gave
    nothing usable) instead of silently treating them as good data.
  - Circuit breaker: if a worker hits many consecutive failures in a row (own IP
    likely got rate-limited/blocked), it backs off instead of blasting through the
    rest of the list uselessly.
  - Full operator visibility for a long unattended run: rotating file log +
    a single _run_summary.csv at the end (domain, status, pages, chars, emails,
    duration, error) so you can audit an 850-domain run without reading JSON files
    one by one.
  - PDF support -- discovered PDF links (team-roster/one-pager downloads) get
    their text extracted directly, same as Firecrawl.
  - Markdown output mode (--markdown) -- matches Firecrawl's actual output format
    (headings/links/emphasis kept as markdown) instead of flattened plain text.
  - Optional proxy routing (--proxy / CRAWLER_PROXY_URL) -- Firecrawl distributes
    requests across many IPs; this lets you plug in your own proxy for the same
    reason (avoiding one-IP-hits-many-domains rate-limiting patterns).
  - Screenshot capture (--screenshot) -- Firecrawl offers page screenshots; this
    now does too, saved as real PNGs per page.

Deliberately NOT attempted (this is a boundary, not a gap): bypassing hard
anti-bot infrastructure (enterprise Cloudflare challenge, PerimeterX, DataDome).
Firecrawl's paid tier does this; matching it here would mean either paying for
a CAPTCHA-solving service or building adversarial bypass automation that
crosses into ToS-violating territory. Blocked sites are detected and reported
cleanly (status=blocked) instead.

Usage:
    python crawl.py --domain example.com
    python crawl.py --domains-file leads.csv --domain-column website --out output/
    python crawl.py --domains-file leads.csv --domain-column website --workers 3
    python crawl.py --domain example.com --save-html --headful   # debug a stubborn site

Output per domain: output/<domain>/crawl_result.json
    {
      "domain": "example.com",
      "crawled_at": "2026-09-08T12:00:00+00:00",
      "status": "ok" | "crawl_failed" | "blocked" | "robots_disallowed",
      "pages": [ {"url": "...", "page_type": "home", "text": "...", "chars": 1234, "low_content": false} ],
      "emails": ["sales@example.com"],
      "total_chars": 12345,
      "duration_sec": 8.4,
      "error": null
    }

Downstream note: treat any status other than "ok" as "not usable" for drafting --
never fabricate personalization from a failed/blocked/disallowed crawl.
"""

import argparse
import concurrent.futures
import csv
import json
import logging
import logging.handlers
import os
import random
import re
import sys
import threading
import time
import urllib.robotparser as robotparser
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import pypdf
import requests
import tldextract
import trafilatura
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ---------------------------------------------------------------------------
# Config (overridable via CLI flags in main())
# ---------------------------------------------------------------------------

MAX_PAGES_PER_DOMAIN = 8
MAX_TOTAL_CHARS = 60_000
NAV_TIMEOUT_MS = 20_000
POPUP_TIMEOUT_MS = 2_500
CACHE_DAYS = 30
SAVE_RAW_HTML = False
RESPECT_ROBOTS = True
HEADLESS = True
MARKDOWN_OUTPUT = False
PROXY_URL = None  # e.g. "http://user:pass@host:port" -- None = no proxy
SAVE_SCREENSHOTS = False
LOW_CONTENT_CHARS = 150          # below this on the homepage, flag low_content
CIRCUIT_BREAKER_THRESHOLD = 8    # consecutive failures before a worker cools down
CIRCUIT_BREAKER_COOLDOWN_SEC = 60
RETRY_MAX_ATTEMPTS = 2
RETRY_BASE_DELAY_SEC = 1.5

BLOCKED_RESOURCE_TYPES = {"image", "media", "font"}

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
]
# used for our own robots.txt / sitemap.xml fetches (identify honestly, unlike the
# rotated browser UA above which exists only to avoid naive fingerprint blocks)
ROBOTS_USER_AGENT = "ColdEmailLeadCrawler/1.0 (+self-hosted, no-Firecrawl)"

STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
window.chrome = window.chrome || { runtime: {} };
"""

# page_type -> regex patterns matched against link href or visible text
PAGE_PATTERNS = {
    "about": re.compile(r"\babout\b|\bwho[-\s]?we[-\s]?are\b|\bour[-\s]?story\b", re.I),
    # separate from "about" on purpose -- this is the single highest-value page
    # for named decision-makers (Apollo's whole database is built on exactly
    # this kind of name+title data), and a lot of sites keep it as its own page
    "team": re.compile(r"\bteam\b|\bleadership\b|\bmeet[-\s]?the[-\s]?team\b|\bour[-\s]?people\b|\bexecutives?\b|\bmanagement\b|\bfounders?\b", re.I),
    "services": re.compile(r"\bservices?\b|\bpricing\b|\bsolutions?\b|\bwhat[-\s]?we[-\s]?do\b", re.I),
    "contact": re.compile(r"\bcontact\b|\bget[-\s]?in[-\s]?touch\b", re.I),
    "careers": re.compile(r"\bcareers?\b|\bjobs?\b|\bwe[-\s]?are[-\s]?hiring\b", re.I),
    "blog": re.compile(r"\bblog\b|\bnews\b|\binsights?\b|\barticles?\b", re.I),
}

POPUP_BUTTON_TEXTS = [
    "Accept All", "Accept all", "Accept All Cookies", "I Accept", "I Agree",
    "Allow all", "Allow All", "Allow all cookies", "Got it", "Got It",
    "OK", "Ok", "Agree", "Close", "No thanks", "Dismiss", "×", "X",
]
POPUP_SELECTORS = ["[aria-label='Close']", ".modal-close", ".close-button", "button.close"]

# Signals a page is a bot-block / CAPTCHA wall, not real content. Checked against
# the page title + first ~3000 chars of text so we don't false-positive on a page
# that merely mentions "captcha" deep in unrelated copy.
BLOCK_SIGNALS = re.compile(
    r"just a moment|attention required|access denied|are you a human|"
    r"verify you are human|checking your browser|cf-browser-verification|"
    r"unusual traffic from your computer|please enable cookies to continue|"
    r"pardon our interruption|request blocked",
    re.I,
)

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

# Placeholder/doc-example/tracking domains that show up in code snippets, privacy
# policies, and third-party widgets -- never real contact addresses.
JUNK_EMAIL_DOMAINS = {
    "example.com", "example.org", "example.net", "yourcompany.com",
    "yourdomain.com", "domain.com", "test.com", "email.com", "company.com",
    "sentry.io", "wixpress.com", "godaddy.com", "gravatar.com",
    "schema.org", "w3.org", "cloudflare.com",
}

# --- named-person extraction (Apollo-style "who works here" data, for free) -----
#
# Verified live on buffer.com's team page: names sit inside interactive elements
# like <h3><button>Joel Gascoigne</button></h3>, with the title as a SIBLING
# <p>CEO & Co-Founder</p> in a different container. trafilatura's clean-text
# extraction correctly discards this as UI chrome (it's tuned for article prose,
# not card grids) -- meaning the "text" field alone silently loses almost every
# name on a typical team page. This is a separate, structural pass over the raw
# HTML to recover exactly that.
TEAM_TITLE_RE = re.compile(
    r"\b(Co-?Founder|Founder|Chief [A-Za-z]+ Officer|CEO|CTO|CFO|COO|President|Owner|"
    r"Managing Director|Director|Head of [A-Za-z &]+|Sales Manager|VP of [A-Za-z &]+|"
    r"Business Development|Marketing Manager|General Manager|Manager|Engineer|Lead|"
    r"Specialist|Coordinator|Analyst|Consultant|Executive|Representative|Advocate)\b",
    re.I,
)
# "Joel Gascoigne" / "Jo O'Brien-Smith" -- 2-3 Title-Case words, allows a middle
# initial, hyphens and apostrophes in surnames. Deliberately strict (exactly this
# shape) to keep false positives low on arbitrary card/button/div text.
TEAM_NAME_RE = re.compile(r"^[A-Z][a-zA-Z'-]+(?:\s[A-Z]\.)?\s[A-Z][a-zA-Z'-]+$")
# Two-Title-Case-word UI phrases that would otherwise false-positive as names.
TEAM_NAME_STOPWORDS = {
    "our", "about", "contact", "learn", "get", "free", "read", "see", "view",
    "meet", "join", "sign", "log", "book", "start", "watch", "shop", "buy",
    "the", "our", "team", "company", "privacy", "policy", "terms", "service",
    "cookie", "settings", "all", "rights", "reserved", "back", "next", "skip",
}


def extract_team_members_from_html(html: str, max_people: int = 30) -> list:
    """Structural extraction of name+title pairs from raw HTML -- independent
    of trafilatura's prose extraction, which drops this data (see above)."""
    soup = BeautifulSoup(html, "lxml")
    candidates = []  # [("name"|"title", text, doc_order_index)]
    for el in soup.find_all(True):
        own_text = "".join(el.find_all(string=True, recursive=False)).strip()
        own_text = re.sub(r"\s+", " ", own_text)
        if not own_text or len(own_text) > 60:
            continue
        # check title-keyword match FIRST: a role phrase like "Senior Engineer"
        # is also shaped like two Title-Case words, so without this order a
        # title gets misread as a person's name (verified live on buffer.com --
        # "Senior Engineer" was misfiled as a name before this fix)
        if len(own_text.split()) <= 6 and TEAM_TITLE_RE.search(own_text):
            candidates.append(("title", own_text))
        elif TEAM_NAME_RE.match(own_text):
            words = [w.lower().strip(".") for w in own_text.split()]
            if any(w in TEAM_NAME_STOPWORDS for w in (words[0], words[-1])):
                continue
            candidates.append(("name", own_text))

    people = []
    seen_names = set()
    for i, (kind, text) in enumerate(candidates):
        if kind != "name" or text.lower() in seen_names:
            continue
        # look ahead a few candidates for the paired title; bail if another
        # name shows up first (that title belongs to the next person, not this one)
        for kind2, text2 in candidates[i + 1: i + 6]:
            if kind2 == "name":
                break
            if kind2 == "title":
                people.append({"name": text, "title": text2})
                seen_names.add(text.lower())
                break
        if len(people) >= max_people:
            break
    return people


def extract_jsonld_people(html: str) -> list:
    """Schema.org JSON-LD structured data (<script type="application/ld+json">)
    is a much higher-precision source than DOM-shape guessing when present --
    it's the site explicitly labeling "these are our founders/employees" for
    search engines. Verified live on stripe.com: its homepage JSON-LD names
    real founders (Patrick Collison, John Collison) via Organization.founders,
    on the SAME page where card-shape scraping picked up a fake name ("Zenith
    Zen") from a product-mockup graphic -- exactly why this is a separate,
    more trustworthy channel, not a replacement for the card-based one."""
    people = []
    seen = set()
    for script in re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.I | re.S,
    ):
        try:
            data = json.loads(script.strip())
        except Exception:
            continue
        nodes = data.get("@graph", [data]) if isinstance(data, dict) else data if isinstance(data, list) else []
        for node in nodes:
            if not isinstance(node, dict):
                continue
            for field in ("founder", "founders", "employee", "employees", "member", "members"):
                value = node.get(field)
                if not value:
                    continue
                people_list = value if isinstance(value, list) else [value]
                for p in people_list:
                    if not isinstance(p, dict) or p.get("@type") != "Person":
                        continue
                    name = p.get("name", "").strip()
                    if not name or name.lower() in seen:
                        continue
                    seen.add(name.lower())
                    title = p.get("jobTitle", "") or field.rstrip("s").capitalize()
                    people.append({"name": name, "title": title})
    return people


log = logging.getLogger("crawler")


def setup_logging(out_dir: Path):
    """Console + rotating file handler, so a long unattended run is auditable."""
    log.setLevel(logging.INFO)
    log.handlers.clear()
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    log.addHandler(console)

    log_dir = out_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "crawl.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    log.addHandler(file_handler)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def normalize_domain(raw: str) -> str:
    """Turn 'https://www.Example.com/foo?x=1' -> 'example.com'."""
    raw = raw.strip()
    if not raw:
        return ""
    if not raw.startswith("http"):
        raw = "https://" + raw
    ext = tldextract.extract(raw)
    if not ext.domain or not ext.suffix:
        return ""
    return f"{ext.domain}.{ext.suffix}".lower()


def root_url(domain: str, scheme: str = "https") -> str:
    return f"{scheme}://{domain}"


def classify_error(exc: Exception) -> str:
    """'permanent' (don't retry) vs 'transient' (retry with backoff)."""
    msg = str(exc)
    permanent_markers = ("ERR_NAME_NOT_RESOLVED", "ERR_ADDRESS_UNREACHABLE", "ERR_INVALID_URL")
    if any(m in msg for m in permanent_markers):
        return "permanent"
    return "transient"


def dismiss_popups(page):
    """Best-effort click on common cookie/consent/modal buttons. Never raises."""
    for text in POPUP_BUTTON_TEXTS:
        try:
            btn = page.get_by_text(text, exact=False).first
            if btn.is_visible(timeout=300):
                btn.click(timeout=POPUP_TIMEOUT_MS)
                page.wait_for_timeout(300)
        except Exception:
            continue
    for sel in POPUP_SELECTORS:
        try:
            el = page.locator(sel).first
            if el.is_visible(timeout=300):
                el.click(timeout=POPUP_TIMEOUT_MS)
        except Exception:
            continue


def scroll_to_bottom(page, steps: int = 4):
    """Trigger lazy-loaded content."""
    try:
        for _ in range(steps):
            page.mouse.wheel(0, 2000)
            page.wait_for_timeout(250)
    except Exception:
        pass


def block_heavy_resources(route):
    if route.request.resource_type in BLOCKED_RESOURCE_TYPES:
        route.abort()
    else:
        route.continue_()


def fetch_rendered_html_once(context, url: str, screenshot_path: str = None) -> tuple[str | None, str | None]:
    """Single attempt to load a page. Returns (html, None) or (None, error_class).
    screenshot_path, when given, captures a screenshot before the page closes --
    has to happen here, not by the caller, since the page object doesn't
    survive past this function's return."""
    page = context.new_page()
    page.route("**/*", block_heavy_resources)
    try:
        page.goto(url, timeout=NAV_TIMEOUT_MS, wait_until="domcontentloaded")
        page.wait_for_timeout(800)
        dismiss_popups(page)
        scroll_to_bottom(page)
        html = page.content()
        if screenshot_path:
            capture_screenshot(page, screenshot_path)
        return html, None
    except PWTimeout:
        return None, "transient"
    except Exception as e:
        return None, classify_error(e)
    finally:
        page.close()


def fetch_with_retry(context, url: str, max_attempts: int = RETRY_MAX_ATTEMPTS,
                      screenshot_path: str = None) -> tuple[str | None, str | None]:
    """Retry transient failures with exponential backoff + jitter. Never retries
    a permanent failure (e.g. domain doesn't exist) -- that would just waste time."""
    last_err = None
    for attempt in range(max_attempts):
        html, err = fetch_rendered_html_once(context, url, screenshot_path=screenshot_path)
        if html is not None:
            return html, None
        last_err = err
        if err == "permanent":
            break
        if attempt < max_attempts - 1:
            delay = RETRY_BASE_DELAY_SEC * (2 ** attempt) + random.uniform(0, 0.5)
            log.warning(f"  transient error on {url}, retrying in {delay:.1f}s")
            time.sleep(delay)
    if last_err:
        log.warning(f"  gave up on {url} ({last_err})")
    return None, last_err


def looks_blocked(html: str) -> bool:
    """Detect a CAPTCHA/bot-block wall vs. real content."""
    sample = BeautifulSoup(html, "lxml").get_text(" ")[:3000]
    return bool(BLOCK_SIGNALS.search(sample))


def get_robots_parser(domain: str) -> robotparser.RobotFileParser:
    rp = robotparser.RobotFileParser()
    try:
        r = requests.get(f"https://{domain}/robots.txt", timeout=5,
                          headers={"User-Agent": ROBOTS_USER_AGENT})
        rp.parse(r.text.splitlines() if r.status_code == 200 else [])
    except Exception:
        rp.parse([])  # unreachable robots.txt -> treat as allow-all, don't block a whole crawl on it
    return rp


def robots_allows(rp: robotparser.RobotFileParser, url: str) -> bool:
    if not RESPECT_ROBOTS:
        return True
    try:
        return rp.can_fetch(ROBOTS_USER_AGENT, url)
    except Exception:
        return True


def discover_links(base_url: str, html: str) -> dict:
    """Find candidate links per page_type from the rendered nav/footer.
    Returns page_type -> list of urls (blog can have multiple candidates).

    Matches against the URL PATH first (nav links have clean paths like
    /team or /about-us), and only falls back to link TEXT when that text is
    short (<=4 words, i.e. reads like a menu item, not a sentence). Matching
    against a link's full sentence-length text is how a common word like
    "team" false-positives on marketing copy such as "...for the marketing
    team" or "about" false-positives on "learn more about our pricing" --
    this was verified live (buffer.com matched an unrelated "higher
    education" page as "team" before this fix)."""
    soup = BeautifulSoup(html, "lxml")
    found: dict[str, list[str]] = {}
    seen_urls = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = a.get_text(strip=True) or ""
        path = urlparse(href).path
        text_is_nav_like = len(text.split()) <= 4
        for page_type, pattern in PAGE_PATTERNS.items():
            matched = pattern.search(path) or (text_is_nav_like and pattern.search(text))
            if not matched:
                continue
            # strip query string (?cta=..., ?utm_=...) before dedup -- otherwise
            # the same page with two different tracking params burns two of the
            # 8-page budget on identical content. Verified live: buffer.com's
            # /insights linked twice with different cta= params, wasting a slot
            # that should go to an actually-distinct page.
            full = urljoin(base_url, href).split("?", 1)[0].split("#", 1)[0]
            if urlparse(full).netloc != urlparse(base_url).netloc:
                continue
            if full in seen_urls:
                continue
            cap = 2 if page_type == "blog" else 1
            bucket = found.setdefault(page_type, [])
            if len(bucket) < cap:
                bucket.append(full)
                seen_urls.add(full)
    return found


def discover_via_sitemap(domain: str) -> dict:
    """Fallback/supplement discovery for sites whose nav is JS-router-hidden or
    behind a hamburger menu that our rendered-HTML pass didn't expose links for."""
    found: dict[str, list[str]] = {}
    for sm_path in ("/sitemap.xml", "/sitemap_index.xml"):
        try:
            r = requests.get(f"https://{domain}{sm_path}", timeout=5,
                              headers={"User-Agent": ROBOTS_USER_AGENT})
            if r.status_code != 200 or "<loc>" not in r.text:
                continue
            locs = re.findall(r"<loc>(.*?)</loc>", r.text)[:300]
            for raw_loc in locs:
                # the sitemap spec requires absolute URLs in <loc>, but real
                # sites don't always comply -- verified live on basecamp.com,
                # whose sitemap uses bare paths like "/gettingreal" instead of
                # "https://basecamp.com/gettingreal". urljoin handles both:
                # a relative path gets resolved against the domain, an
                # already-absolute URL passes through unchanged.
                loc = urljoin(f"https://{domain}", raw_loc.strip())
                path_text = urlparse(loc).path
                for page_type, pattern in PAGE_PATTERNS.items():
                    if pattern.search(path_text):
                        cap = 2 if page_type == "blog" else 1
                        bucket = found.setdefault(page_type, [])
                        if len(bucket) < cap and loc not in bucket:
                            bucket.append(loc)
            if found:
                break
        except Exception:
            continue
    return found


def merge_discovery(nav_found: dict, sitemap_found: dict) -> dict:
    """Nav links take priority (usually more accurate); sitemap fills gaps and
    tops up blog to 2 posts if nav only surfaced one."""
    merged = {k: list(v) for k, v in nav_found.items()}
    for page_type, urls in sitemap_found.items():
        cap = 2 if page_type == "blog" else 1
        bucket = merged.setdefault(page_type, [])
        for u in urls:
            if len(bucket) >= cap:
                break
            if u not in bucket:
                bucket.append(u)
    return merged


def extract_clean_text(html: str, url: str, markdown: bool = False) -> str:
    """The 'clean text out' part -- strips nav/ads/boilerplate. markdown=True
    matches Firecrawl's actual output format (headings, links, emphasis kept
    as markdown syntax) instead of flattened plain text."""
    text = trafilatura.extract(
        html,
        url=url,
        include_comments=False,
        include_tables=True,
        favor_precision=True,
        output_format="markdown" if markdown else "txt",
    )
    return text or ""


def is_pdf_url(url: str) -> bool:
    """True if the URL points at a PDF -- Firecrawl extracts these
    (team-roster PDFs, downloadable one-pagers); a plain HTML-only crawler
    would silently fail or skip them."""
    return urlparse(url).path.lower().endswith(".pdf")


def fetch_pdf_bytes(url: str) -> bytes | None:
    """Plain HTTP GET for a PDF's raw bytes -- no need for Playwright here,
    a PDF isn't JS-rendered."""
    try:
        r = requests.get(url, timeout=15, headers={"User-Agent": ROBOTS_USER_AGENT})
        if r.status_code != 200:
            return None
        return r.content
    except Exception:
        return None


def extract_pdf_text(pdf_bytes: bytes) -> str:
    """Best-effort text extraction from a PDF's raw bytes. Returns '' on a
    corrupt/unreadable PDF rather than raising -- one bad PDF must not crash
    the rest of a domain's crawl."""
    try:
        import io
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        chunks = [page.extract_text() or "" for page in reader.pages]
        return "\n".join(chunks).strip()
    except Exception:
        return ""


def extract_emails(html: str, site_domain: str = "") -> list:
    """Pull real contact emails, dropping placeholder/example/tracking junk.

    site_domain, when given, sorts emails on that domain first -- those are
    almost always the genuine contact, vs. a third-party widget address.
    """
    emails = set()
    soup = BeautifulSoup(html, "lxml")
    for a in soup.find_all("a", href=True):
        if a["href"].lower().startswith("mailto:"):
            addr = a["href"][7:].split("?")[0].strip()
            if EMAIL_RE.fullmatch(addr):
                emails.add(addr.lower())
    for m in EMAIL_RE.findall(soup.get_text(" ")):
        emails.add(m.lower())

    def is_real(addr: str) -> bool:
        if re.search(r"\.(png|jpg|jpeg|gif|svg)$", addr):
            return False
        email_domain = addr.rsplit("@", 1)[-1]
        if email_domain in JUNK_EMAIL_DOMAINS:
            return False
        if re.search(r"^(example|test|sample|yourcompany|yourdomain)\b", email_domain):
            return False
        return True

    cleaned = [e for e in emails if is_real(e)]
    if site_domain:
        cleaned.sort(key=lambda e: (not e.endswith("@" + site_domain), e))
    else:
        cleaned.sort()
    return cleaned


def cache_path(out_dir: Path, domain: str) -> Path:
    return out_dir / domain / "crawl_result.json"


def is_cache_fresh(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        crawled_at = datetime.fromisoformat(data["crawled_at"])
        age_days = (datetime.now(timezone.utc) - crawled_at).days
        return age_days < CACHE_DAYS and data.get("status") == "ok"
    except Exception:
        return False


def capture_screenshot(page, path: str) -> bool:
    """Firecrawl offers page screenshots; this didn't at all before. Never
    raises -- a screenshot failure (page closed, out of memory) must not
    take down the rest of a domain's crawl over what's a nice-to-have."""
    try:
        page.screenshot(path=path)
        return True
    except Exception:
        return False


def build_context_kwargs(proxy_url: str = None) -> dict:
    """Playwright new_context() kwargs, with optional proxy routing --
    Firecrawl distributes requests across many IPs; we crawl from one by
    default. When a proxy IS supplied (user's own, not something this tool
    sources), credentials embedded in the URL (http://user:pass@host:port)
    are split out into Playwright's separate username/password fields,
    since its proxy dict expects the server URL without embedded auth."""
    kwargs = {
        "user_agent": random.choice(USER_AGENTS),
        "viewport": {"width": 1366, "height": 900},
        "locale": "en-US",
        "ignore_https_errors": True,  # small business sites frequently have expired/self-signed certs
        "extra_http_headers": {"Accept-Language": "en-US,en;q=0.9"},
    }
    if proxy_url:
        parsed = urlparse(proxy_url)
        server = f"{parsed.scheme}://{parsed.hostname}"
        if parsed.port:
            server += f":{parsed.port}"
        proxy_config = {"server": server}
        if parsed.username:
            proxy_config["username"] = parsed.username
        if parsed.password:
            proxy_config["password"] = parsed.password
        kwargs["proxy"] = proxy_config
    return kwargs


# ---------------------------------------------------------------------------
# Core: crawl one domain
# ---------------------------------------------------------------------------

def crawl_domain(domain: str, out_dir: Path, browser, force: bool = False) -> dict:
    domain = normalize_domain(domain)
    if not domain:
        return {"domain": domain, "status": "crawl_failed", "error": "invalid domain",
                "pages": [], "emails": [], "total_chars": 0, "duration_sec": 0}

    out_path = cache_path(out_dir, domain)
    if not force and is_cache_fresh(out_path):
        log.info(f"[{domain}] cache hit, skipping")
        return json.loads(out_path.read_text(encoding="utf-8"))

    log.info(f"[{domain}] crawling...")
    start = time.monotonic()

    context = browser.new_context(**build_context_kwargs(PROXY_URL))
    context.add_init_script(STEALTH_INIT_SCRIPT)

    result = {
        "domain": domain,
        "crawled_at": datetime.now(timezone.utc).isoformat(),
        "status": "crawl_failed",
        "pages": [],
        "emails": [],
        "team_members": [],
        "total_chars": 0,
        "duration_sec": 0,
        "error": None,
    }

    try:
        rp = get_robots_parser(domain)
        home_url = root_url(domain)

        if not robots_allows(rp, home_url):
            result["status"] = "robots_disallowed"
            result["error"] = "robots.txt disallows crawling this domain"
            return result

        def shot_path(page_type: str) -> str:
            if not SAVE_SCREENSHOTS:
                return None
            shots_dir = out_path.parent / "screenshots"
            shots_dir.mkdir(parents=True, exist_ok=True)
            safe_name = re.sub(r"[^a-zA-Z0-9_-]", "_", page_type)[:40]
            return str(shots_dir / f"{safe_name}.png")

        home_html, err = fetch_with_retry(context, home_url, screenshot_path=shot_path("home"))
        if home_html is None:
            # try www. and plain-http as last-resort variants before giving up --
            # some smaller sites only redirect one direction, or have broken https
            for fallback_url in (root_url("www." + domain), root_url(domain, scheme="http")):
                home_html, err = fetch_with_retry(context, fallback_url, max_attempts=1,
                                                   screenshot_path=shot_path("home"))
                if home_html is not None:
                    home_url = fallback_url
                    break

        if home_html is None:
            result["error"] = f"homepage unreachable after retries ({err or 'unknown'})"
            # fall through to write -- a failed crawl must still be persisted so the
            # orchestrator can see status and route to manual_review, not retry forever
        elif looks_blocked(home_html):
            result["status"] = "blocked"
            result["error"] = "bot-detection/CAPTCHA wall on homepage -- will not pass on retry"
        else:
            nav_found = discover_links(home_url, home_html)
            sitemap_found = discover_via_sitemap(domain)
            discovered = merge_discovery(nav_found, sitemap_found)

            pages_to_fetch = [("home", home_url)]
            for page_type, urls in discovered.items():
                for u in urls:
                    pages_to_fetch.append((page_type, u))
            pages_to_fetch = pages_to_fetch[:MAX_PAGES_PER_DOMAIN]

            all_emails = set(extract_emails(home_html, domain))
            all_team_members = []
            seen_team_names = set()
            total_chars = 0
            blocked_pages = []
            robots_skipped = []

            for page_type, url in pages_to_fetch:
                if total_chars >= MAX_TOTAL_CHARS:
                    break

                # PDFs (team-roster PDFs, downloadable one-pagers) aren't
                # JS-rendered pages -- fetch raw bytes and extract text
                # directly instead of routing through Playwright/trafilatura.
                if is_pdf_url(url):
                    if not robots_allows(rp, url):
                        robots_skipped.append(url)
                        continue
                    pdf_bytes = fetch_pdf_bytes(url)
                    time.sleep(0.5 + random.uniform(0, 0.4))
                    if not pdf_bytes:
                        continue
                    pdf_text = extract_pdf_text(pdf_bytes)
                    if not pdf_text:
                        continue
                    remaining = MAX_TOTAL_CHARS - total_chars
                    pdf_text = pdf_text[:remaining]
                    total_chars += len(pdf_text)
                    all_emails.update(extract_emails(pdf_text, domain))
                    result["pages"].append({
                        "url": url, "page_type": page_type, "text": pdf_text,
                        "chars": len(pdf_text), "low_content": False, "source_type": "pdf",
                    })
                    continue

                if page_type == "home":
                    html = home_html
                else:
                    if not robots_allows(rp, url):
                        robots_skipped.append(url)
                        continue
                    html, _ = fetch_with_retry(context, url, max_attempts=1, screenshot_path=shot_path(page_type))
                    time.sleep(0.5 + random.uniform(0, 0.4))  # polite jittered delay, same domain
                if html is None:
                    continue
                if looks_blocked(html):
                    blocked_pages.append(url)
                    continue

                # structural name+title extraction from raw HTML -- runs even if
                # trafilatura below finds no prose text, since team member cards
                # often ARE the entire content of a team page (see module note).
                # Deliberately NOT run on "home" -- verified live on stripe.com
                # that a homepage product-mockup graphic contains fake dummy
                # names ("Zenith Zen") shaped exactly like a real name+title
                # pair. team/about pages don't carry that risk.
                if page_type in ("team", "about"):
                    for person in extract_team_members_from_html(html):
                        key = person["name"].lower()
                        if key not in seen_team_names:
                            seen_team_names.add(key)
                            all_team_members.append(person)

                # JSON-LD structured data is higher-precision (the site is
                # explicitly labeling "these are our founders/employees" for
                # search engines) so this one IS safe to run on every page,
                # home included -- it isn't guessing from DOM shape.
                for person in extract_jsonld_people(html):
                    key = person["name"].lower()
                    if key not in seen_team_names:
                        seen_team_names.add(key)
                        all_team_members.append(person)

                # Email + raw-HTML capture must NOT depend on trafilatura finding
                # prose text below -- verified live: a sparse contact page (just
                # "Email: sales@x.com / Phone: ..." with no article-style prose)
                # can legitimately return empty from extract_clean_text, and this
                # used to silently drop that page's real mailto emails entirely.
                # Raw-HTML save is unconditional too, for the same reason --
                # otherwise the exact pages worth debugging (why did extraction
                # find nothing here?) are the ones --save-html can't show you.
                all_emails.update(extract_emails(html, domain))

                if SAVE_RAW_HTML:
                    raw_dir = out_path.parent / "raw"
                    raw_dir.mkdir(parents=True, exist_ok=True)
                    safe_name = re.sub(r"[^a-zA-Z0-9_-]", "_", page_type)[:40]
                    (raw_dir / f"{safe_name}.html").write_text(html, encoding="utf-8")

                text = extract_clean_text(html, url, markdown=MARKDOWN_OUTPUT)
                if not text:
                    continue
                remaining = MAX_TOTAL_CHARS - total_chars
                text = text[:remaining]
                total_chars += len(text)

                page_entry = {
                    "url": url,
                    "page_type": page_type,
                    "text": text,
                    "chars": len(text),
                    "low_content": page_type == "home" and len(text) < LOW_CONTENT_CHARS,
                }
                result["pages"].append(page_entry)

            result["emails"] = sorted(all_emails, key=lambda e: (not e.endswith("@" + domain), e))
            result["team_members"] = all_team_members[:40]
            result["total_chars"] = total_chars
            if blocked_pages:
                result["blocked_pages"] = blocked_pages
            if robots_skipped:
                result["robots_skipped"] = robots_skipped

            # "ok" if we got EITHER usable prose text OR real contact data
            # (emails / named people) -- a thin small-business site can
            # legitimately have zero article-style prose anywhere yet still
            # have a working "Email: sales@x.com" on its contact page. That's
            # real, actionable data for Stage 4; marking it crawl_failed would
            # make contacts.py skip the domain entirely and throw it away.
            result["status"] = "ok" if (result["pages"] or result["emails"] or result["team_members"]) else "crawl_failed"
            if result["status"] == "crawl_failed":
                result["error"] = "no extractable text, emails, or named people found on any reachable page"

    except Exception as e:
        result["error"] = f"unexpected error: {e}"
        log.error(f"[{domain}] {e}")
    finally:
        context.close()

    result["duration_sec"] = round(time.monotonic() - start, 2)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info(
        f"[{domain}] status={result['status']} pages={len(result['pages'])} "
        f"chars={result['total_chars']} emails={len(result['emails'])} "
        f"took={result['duration_sec']}s"
    )
    return result


# ---------------------------------------------------------------------------
# Batch runner
# ---------------------------------------------------------------------------

def load_domains_from_csv(path: str, column: str) -> list:
    domains = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if column not in reader.fieldnames:
            raise SystemExit(f"Column '{column}' not found. Available columns: {reader.fieldnames}")
        for row in reader:
            val = (row.get(column) or "").strip()
            if val:
                domains.append(val)
    return domains


class Progress:
    """Thread-safe counter shared across worker chunks for periodic progress logs."""

    def __init__(self, total: int):
        self.total = total
        self.done = 0
        self.lock = threading.Lock()

    def tick(self):
        with self.lock:
            self.done += 1
            if self.done % 10 == 0 or self.done == self.total:
                log.info(f"[progress] {self.done}/{self.total} domains processed")


def _crawl_chunk(domain_chunk: list, out_dir: Path, progress: Progress, force: bool) -> list:
    """Runs in its own thread with its own Playwright + browser instance.

    Playwright's sync API is NOT thread-safe -- one browser object cannot be
    shared across threads (breaks with 'Failed to find browser context').
    So each worker gets a fully independent playwright/browser of its own.

    Also runs a circuit breaker: if this worker's own IP gets rate-limited or
    blocked, hammering the rest of its chunk just burns time for zero results --
    back off and give the network a chance to recover instead.
    """
    results = []
    consecutive_failures = 0
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=HEADLESS,
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            for d in domain_chunk:
                try:
                    r = crawl_domain(d, out_dir, browser, force=force)
                    results.append(r)
                    if r["status"] == "ok":
                        consecutive_failures = 0
                    else:
                        consecutive_failures += 1
                except Exception as e:
                    log.error(f"[{d}] worker crashed: {e}")
                    results.append({"domain": d, "status": "crawl_failed", "error": str(e),
                                     "pages": [], "emails": [], "total_chars": 0, "duration_sec": 0})
                    consecutive_failures += 1
                finally:
                    progress.tick()

                if consecutive_failures >= CIRCUIT_BREAKER_THRESHOLD:
                    log.warning(
                        f"{consecutive_failures} consecutive failures -- possible IP "
                        f"rate-limit/block. Cooling down {CIRCUIT_BREAKER_COOLDOWN_SEC}s."
                    )
                    time.sleep(CIRCUIT_BREAKER_COOLDOWN_SEC)
                    consecutive_failures = 0
        finally:
            browser.close()
    return results


def write_run_summary(out_dir: Path, all_results: list):
    summary_path = out_dir / "_run_summary.csv"
    fields = ["domain", "status", "pages", "total_chars", "emails_found", "duration_sec", "error"]
    with open(summary_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for r in all_results:
            writer.writerow({
                "domain": r.get("domain", ""),
                "status": r.get("status", ""),
                "pages": len(r.get("pages", [])),
                "total_chars": r.get("total_chars", 0),
                "emails_found": len(r.get("emails", [])),
                "duration_sec": r.get("duration_sec", 0),
                "error": r.get("error") or "",
            })
    log.info(f"Run summary written: {summary_path}")


def run_batch(domains: list, out_dir: Path, workers: int, force: bool = False):
    out_dir.mkdir(parents=True, exist_ok=True)
    progress = Progress(len(domains))
    log.info(f"Starting crawl of {len(domains)} domains, {workers} concurrent worker(s), "
             f"each with its own browser instance.")

    all_results = []
    if workers <= 1:
        all_results = _crawl_chunk(domains, out_dir, progress, force)
    else:
        chunks = [domains[i::workers] for i in range(workers)]
        chunks = [c for c in chunks if c]
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(chunks)) as pool:
            futures = [pool.submit(_crawl_chunk, chunk, out_dir, progress, force) for chunk in chunks]
            for fut in concurrent.futures.as_completed(futures):
                all_results.extend(fut.result())

    summary = {"ok": 0, "failed": 0, "blocked": 0, "robots_disallowed": 0}
    for r in all_results:
        status = r.get("status", "crawl_failed")
        if status == "ok":
            summary["ok"] += 1
        elif status == "blocked":
            summary["blocked"] += 1
        elif status == "robots_disallowed":
            summary["robots_disallowed"] += 1
        else:
            summary["failed"] += 1

    write_run_summary(out_dir, all_results)
    log.info(f"Done. ok={summary['ok']} failed={summary['failed']} "
             f"blocked={summary['blocked']} robots_disallowed={summary['robots_disallowed']} "
             f"out_dir={out_dir}")
    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    global MAX_PAGES_PER_DOMAIN, NAV_TIMEOUT_MS, CACHE_DAYS, SAVE_RAW_HTML, RESPECT_ROBOTS, HEADLESS, MARKDOWN_OUTPUT, PROXY_URL, SAVE_SCREENSHOTS

    ap = argparse.ArgumentParser(description="Free self-hosted Firecrawl replacement.")
    ap.add_argument("--domain", help="single domain/url to crawl")
    ap.add_argument("--domains-file", help="CSV file containing a domain/website column")
    ap.add_argument("--domain-column", default="website", help="column name in --domains-file (default: website)")
    ap.add_argument("--out", default="output", help="output directory (default: output/)")
    ap.add_argument("--workers", type=int, default=2,
                     help="concurrent browsers (default: 2, keep low -- more doesn't speed things up "
                          "much and raises block risk)")
    ap.add_argument("--max-pages", type=int, default=MAX_PAGES_PER_DOMAIN, help="max pages per domain")
    ap.add_argument("--timeout-ms", type=int, default=NAV_TIMEOUT_MS, help="per-page navigation timeout")
    ap.add_argument("--cache-days", type=int, default=CACHE_DAYS, help="skip re-crawling a domain within N days")
    ap.add_argument("--no-cache", action="store_true", help="force re-crawl even if cached")
    ap.add_argument("--save-html", action="store_true", help="save raw HTML per page under output/<domain>/raw/")
    ap.add_argument("--ignore-robots", action="store_true", help="do NOT respect robots.txt (not recommended)")
    ap.add_argument("--headful", action="store_true", help="show the browser window (debugging a stubborn site)")
    ap.add_argument("--markdown", action="store_true",
                     help="output page text as markdown (headings/links/emphasis kept) instead of "
                          "flattened plain text -- matches Firecrawl's actual output format")
    ap.add_argument("--proxy", default=os.environ.get("CRAWLER_PROXY_URL"),
                     help="route requests through this proxy (http://[user:pass@]host:port). "
                          "Also settable via CRAWLER_PROXY_URL env var. Your own proxy -- "
                          "not something this tool sources or manages.")
    ap.add_argument("--screenshot", action="store_true",
                     help="save a PNG screenshot of each page under output/<domain>/screenshots/")
    args = ap.parse_args()

    MAX_PAGES_PER_DOMAIN = args.max_pages
    NAV_TIMEOUT_MS = args.timeout_ms
    CACHE_DAYS = args.cache_days
    SAVE_RAW_HTML = args.save_html
    RESPECT_ROBOTS = not args.ignore_robots
    HEADLESS = not args.headful
    MARKDOWN_OUTPUT = args.markdown
    PROXY_URL = args.proxy
    SAVE_SCREENSHOTS = args.screenshot

    out_dir = Path(args.out)
    setup_logging(out_dir)

    if args.domain:
        domains = [args.domain]
    elif args.domains_file:
        domains = load_domains_from_csv(args.domains_file, args.domain_column)
    else:
        ap.error("pass --domain or --domains-file")
        return

    domains = [d for d in domains if d]
    if not domains:
        log.error("no domains to crawl")
        return

    run_batch(domains, out_dir, args.workers, force=args.no_cache)


if __name__ == "__main__":
    main()
