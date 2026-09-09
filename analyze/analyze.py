"""
Stage 3: Analyze -- turns crawled page text into structured business data
(what they do, pain points, hiring signals, hooks worth writing an email
around) via a single Claude API call per domain, with a deterministic
hallucination clamp: every claim the model makes in recent_events/pains/
hooks must carry a verbatim quote + source_url, and this code VERIFIES that
quote actually appears in the crawled text before trusting it. A claim that
doesn't verify gets dropped, never passed downstream -- "specific and
credible" is the entire value of a personalized cold email; a fabricated
detail is worse than a generic one.

Honest note on testing: this is the first pipeline stage needing a real
Claude API call, and no ANTHROPIC_API_KEY was available in this session's
shell to test against (Claude Code doesn't expose its own credentials to
scripts -- correctly so). Every function here is tested with the real
Anthropic Python SDK mocked at the client boundary, same pattern as
OpenCorporates in contacts/ -- the API-call plumbing (prefill technique,
JSON parsing, error handling) is verified; the actual live extraction
quality is not, since that requires your own key. Spot-check the first
dozen or so real extractions by hand before trusting a big batch -- same
advice the original pipeline design gave for this exact stage.

Output schema (per domain):
    {
      "company_name": "", "what_they_do": "", "icp_they_serve": "",
      "services": [], "geo": "", "size_estimate": "",
      "recent_events": [{"event":"","quote":"","source_url":""}],
      "hiring_signals": [], "tech_signals": [],
      "pains": [{"pain":"","quote":"","source_url":"","confidence":0}],
      "hooks": [{"hook":"","quote":"","source_url":"","specificity":0}],
      "disqualifiers": []
    }

Gate: signal_gate = "low_signal" when there's no hook with specificity >= 7,
or any disqualifier is present -- route these to a shorter, claim-free
template downstream (or skip), never force a personalized angle that isn't
actually there.

Usage:
    python analyze.py --crawl-dir ../crawler/output
    python analyze.py --crawl-dir ../crawler/output --domain stripe.com
    python analyze.py --crawl-dir ../crawler/output --dry-run   # show prompts, call nothing
"""
import argparse
import json
import logging
import logging.handlers
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import anthropic

log = logging.getLogger("analyze")

DEFAULT_MODEL = "claude-sonnet-5"
MAX_PROMPT_CHARS = 60_000
MAX_OUTPUT_TOKENS = 2048
MIN_HOOK_SPECIFICITY_FOR_OK = 7

EXTRACTION_SCHEMA_KEYS = [
    "company_name", "what_they_do", "icp_they_serve", "services", "geo",
    "size_estimate", "recent_events", "hiring_signals", "tech_signals",
    "pains", "hooks", "disqualifiers",
]
_LIST_KEYS = {"services", "recent_events", "hiring_signals", "tech_signals",
              "pains", "hooks", "disqualifiers"}

EXTRACTION_PROMPT_TEMPLATE = """You are analyzing a company's website content to prepare for a personalized cold sales email. Extract ONLY facts that are explicitly present in the text below -- do not invent, infer beyond what's stated, or add generic industry assumptions.

CRITICAL RULE: every item in "recent_events", "pains", and "hooks" MUST include an EXACT VERBATIM quote copied character-for-character from the source text below, plus the exact source_url it came from. A claim without a real, checkable quote will be discarded automatically -- do not skip this requirement.

Return ONLY a JSON object (no markdown, no commentary) with this exact shape:
{{
  "company_name": "",
  "what_they_do": "",
  "icp_they_serve": "",
  "services": [],
  "geo": "",
  "size_estimate": "",
  "recent_events": [{{"event": "", "quote": "", "source_url": ""}}],
  "hiring_signals": [],
  "tech_signals": [],
  "pains": [{{"pain": "", "quote": "", "source_url": "", "confidence": 0}}],
  "hooks": [{{"hook": "", "quote": "", "source_url": "", "specificity": 0}}],
  "disqualifiers": []
}}

"specificity" (1-10) rates how specific/personal a hook is -- a generic industry statement scores low, a concrete detail unique to this company (a named product, a specific hire, a specific recent event) scores high.

--- SOURCE PAGES ---
{pages_text}
--- END SOURCE PAGES ---

Domain being analyzed: {domain}
"""


def setup_logging(out_dir: Path):
    log.setLevel(logging.INFO)
    log.handlers.clear()
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    log.addHandler(console)
    log_dir = out_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    fh = logging.handlers.RotatingFileHandler(log_dir / "analyze.log", maxBytes=5_000_000,
                                               backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

def build_extraction_prompt(crawl_result: dict, max_chars: int = MAX_PROMPT_CHARS) -> str:
    domain = crawl_result.get("domain", "")
    chunks = []
    total = 0
    for page in crawl_result.get("pages", []):
        header = f"\n[URL: {page.get('url', '')}] [page_type: {page.get('page_type', '')}]\n"
        text = page.get("text", "")
        remaining = max_chars - total
        if remaining <= 0:
            break
        chunk = (header + text)[:remaining]
        chunks.append(chunk)
        total += len(chunk)
    pages_text = "\n".join(chunks)
    return EXTRACTION_PROMPT_TEMPLATE.format(pages_text=pages_text, domain=domain)


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

def parse_extraction_response(raw_text: str) -> dict:
    """Never raises. Strips markdown fences if present, fills any missing
    schema key with a type-appropriate default, and returns a
    _parse_error sentinel on genuinely malformed JSON rather than crashing
    the batch over one bad response."""
    cleaned = raw_text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    # tolerate a dangling opening brace from the prefill technique not
    # being included in the raw text passed in (call_claude_extraction
    # re-adds it before calling this, but this function stays robust either way)
    if not cleaned.startswith("{"):
        cleaned = "{" + cleaned

    try:
        data = json.loads(cleaned)
    except Exception:
        return {"_parse_error": True, "_raw_text": raw_text}

    for key in EXTRACTION_SCHEMA_KEYS:
        if key not in data:
            data[key] = [] if key in _LIST_KEYS else ""
    return data


# ---------------------------------------------------------------------------
# Hallucination clamp -- deterministic quote verification
# ---------------------------------------------------------------------------

def normalize_quote(text: str) -> str:
    """Whitespace-collapsed, lowercased, curly-apostrophe-normalized. Real
    bug class hit repeatedly in Stages 2 and 4 this project: a quote using a
    typographic apostrophe (') failing to match crawled text (or vice
    versa) using a straight one ('). Applying that lesson here up front."""
    text = text.replace("’", "'").replace("‘", "'")
    text = re.sub(r"\s+", " ", text.strip().lower())
    return text


def verify_quote_in_sources(quote: str, source_url: str, pages: list) -> bool:
    if not quote or not source_url:
        return False
    normalized_quote = normalize_quote(quote)
    for page in pages:
        if page.get("url") == source_url:
            return normalized_quote in normalize_quote(page.get("text", ""))
    return False


def verify_extraction_quotes(extraction: dict, pages: list) -> dict:
    """Applies the clamp to recent_events, pains, and hooks. Returns the
    extraction with unverified items removed and a quotes_dropped count for
    observability -- a domain where most claims got dropped is a signal the
    model may have been reaching, worth a manual look."""
    result = dict(extraction)
    dropped = 0
    for field in ("recent_events", "pains", "hooks"):
        items = extraction.get(field, []) or []
        kept = []
        for item in items:
            if verify_quote_in_sources(item.get("quote", ""), item.get("source_url", ""), pages):
                kept.append(item)
            else:
                dropped += 1
        result[field] = kept
    result["quotes_dropped"] = dropped
    return result


def compute_signal_gate(extraction: dict) -> str:
    if extraction.get("disqualifiers"):
        return "low_signal"
    hooks = extraction.get("hooks", []) or []
    if any((h.get("specificity") or 0) >= MIN_HOOK_SPECIFICITY_FOR_OK for h in hooks):
        return "ok"
    return "low_signal"


# ---------------------------------------------------------------------------
# Claude API call
# ---------------------------------------------------------------------------

def call_claude_extraction(prompt: str, api_key: str, model: str = DEFAULT_MODEL) -> dict:
    """Never raises -- returns {"_api_error": True, "_error_detail": ...}
    on any failure so a batch run can log-and-continue past one bad call.
    Uses the prefill technique (assistant turn starting with "{") to force
    JSON-only output without relying on the model to avoid markdown fences
    on its own -- more reliable than asking nicely in the prompt alone."""
    if not api_key:
        return {"_api_error": True, "_error_detail": "no API key provided"}
    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=model,
            max_tokens=MAX_OUTPUT_TOKENS,
            messages=[
                {"role": "user", "content": prompt},
                {"role": "assistant", "content": "{"},
            ],
        )
        raw_text = "{" + response.content[0].text
        return parse_extraction_response(raw_text)
    except Exception as e:
        return {"_api_error": True, "_error_detail": str(e)}


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def analyze_domain(crawl_result: dict, api_key: str, model: str = DEFAULT_MODEL) -> dict:
    domain = crawl_result.get("domain", "")
    base = {
        "domain": domain,
        "analyzed_at": datetime.now(timezone.utc).isoformat(),
        "model_used": model,
    }

    if crawl_result.get("status") != "ok":
        return {**base, "status": "no_crawl_data",
                "error": f"upstream crawl status was '{crawl_result.get('status')}', not 'ok'"}

    prompt = build_extraction_prompt(crawl_result)
    extraction = call_claude_extraction(prompt, api_key=api_key, model=model)

    if extraction.get("_api_error"):
        return {**base, "status": "api_error", "error": extraction.get("_error_detail", "unknown API error")}
    if extraction.get("_parse_error"):
        return {**base, "status": "parse_error", "error": "model response was not valid JSON"}

    verified = verify_extraction_quotes(extraction, crawl_result.get("pages", []))
    gate = compute_signal_gate(verified)

    return {
        **base,
        "status": "ok",
        "signal_gate": gate,
        **{k: verified.get(k, [] if k in _LIST_KEYS else "") for k in EXTRACTION_SCHEMA_KEYS},
        "quotes_dropped": verified.get("quotes_dropped", 0),
    }


# ---------------------------------------------------------------------------
# Batch runner
# ---------------------------------------------------------------------------

def find_domains(crawl_dir: Path, domain_filter: set = None) -> list:
    domains = []
    for d in sorted(crawl_dir.iterdir()):
        if not d.is_dir() or not (d / "crawl_result.json").exists():
            continue
        if domain_filter and d.name not in domain_filter:
            continue
        domains.append(d)
    return domains


def run_batch(crawl_dir: Path, api_key: str, model: str = DEFAULT_MODEL,
              domain_filter: set = None, dry_run: bool = False) -> dict:
    domain_dirs = find_domains(crawl_dir, domain_filter)
    if not domain_dirs:
        log.error(f"no crawl_result.json files found under {crawl_dir}")
        return {"ok": 0, "low_signal": 0, "no_crawl_data": 0, "api_error": 0, "parse_error": 0}

    log.info(f"Processing {len(domain_dirs)} domain(s) from {crawl_dir} "
             f"({'DRY RUN -- no API calls' if dry_run else f'model={model}'})")

    summary = {"ok": 0, "low_signal": 0, "no_crawl_data": 0, "api_error": 0, "parse_error": 0}
    for i, d in enumerate(domain_dirs, 1):
        crawl_result = json.loads((d / "crawl_result.json").read_text(encoding="utf-8"))

        if dry_run:
            if crawl_result.get("status") != "ok":
                log.info(f"[{d.name}] would skip -- upstream crawl status is "
                         f"'{crawl_result.get('status')}', not 'ok'")
                continue
            prompt = build_extraction_prompt(crawl_result)
            log.info(f"[{d.name}] would send ~{len(prompt)} chars to {model}")
            continue

        result = analyze_domain(crawl_result, api_key=api_key, model=model)
        (d / "analysis.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")

        status = result["status"]
        if status == "ok":
            summary["ok"] += 1
            if result.get("signal_gate") == "low_signal":
                summary["low_signal"] += 1
            if result.get("quotes_dropped", 0) > 0:
                log.warning(f"[{d.name}] dropped {result['quotes_dropped']} unverified claim(s)")
        else:
            summary[status] = summary.get(status, 0) + 1
            log.warning(f"[{d.name}] {status}: {result.get('error', '')}")

        if i % 10 == 0 or i == len(domain_dirs):
            log.info(f"[progress] {i}/{len(domain_dirs)} domains processed")
        time.sleep(0.2)  # light pacing between API calls

    log.info(f"Done. {summary}")
    return summary


def main():
    ap = argparse.ArgumentParser(description="Stage 3: Analyze -- structured extraction via Claude.")
    ap.add_argument("--crawl-dir", default="../crawler/output",
                     help="output dir from crawl.py (default: ../crawler/output)")
    ap.add_argument("--domain", help="process a single domain only")
    ap.add_argument("--api-key", default=os.environ.get("ANTHROPIC_API_KEY"),
                     help="Anthropic API key (default: ANTHROPIC_API_KEY env var)")
    ap.add_argument("--model", default=DEFAULT_MODEL, help=f"model to use (default: {DEFAULT_MODEL})")
    ap.add_argument("--dry-run", action="store_true", help="show what would be sent, call nothing")
    args = ap.parse_args()

    crawl_dir = Path(args.crawl_dir)
    if not crawl_dir.exists():
        raise SystemExit(f"--crawl-dir not found: {crawl_dir}")

    setup_logging(crawl_dir)

    if not args.dry_run and not args.api_key:
        raise SystemExit("No API key. Set ANTHROPIC_API_KEY or pass --api-key (or use --dry-run).")

    domain_filter = {args.domain} if args.domain else None
    run_batch(crawl_dir, api_key=args.api_key, model=args.model,
              domain_filter=domain_filter, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
