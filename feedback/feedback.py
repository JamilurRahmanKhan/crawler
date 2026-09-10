"""
Stage 10: Feedback loop -- the actual compounding mechanism the whole
pipeline has been building toward. Three real jobs, per the original
design:

  1. Classify genuine replies as positive/negative/neutral (a Claude call,
     layered on top of reply_watcher.py's existing "this is a genuine
     reply, not a bounce/OOO/unsubscribe" classification -- exactly what
     reply_watcher.py's own docstring flagged as "a good use for an LLM
     call... not rebuilt here" until now).
  2. Track metrics per subject-line variant (sent, bounce/reply/positive
     rate) against the original design's healthy benchmarks (bounce <2%,
     reply 5-12%, positive 1-3%), and flag when a variant hasn't reached
     the ~200-send minimum sample size the original design calls for
     before trusting any rate at all.
  3. Take leads that replied POSITIVELY, pull their winning subject/body +
     the verified facts that grounded them, and format as few-shot
     examples draft.py can inject into future prompts -- "here's what
     actually worked for a similar company." That's the real loop.

Real integration, not stubs: reads sender/'s ACTUAL sqlite DB (same file,
same schema this project already writes to -- see db.py's `replies` table,
added specifically to give this stage something real to learn from) and
crawler/output/<domain>/analysis.json (Stage 3's real verified facts).

Uses Google's Gemini API (google-genai SDK), same as every other
LLM-calling stage in this project, at $0 budget. The sentiment
classification call is fully tested with the real google-genai client
mocked at the client boundary, and was ALSO verified live against the
real Gemini API with a real free-tier key; the actual classification
*quality* on a large, varied batch of real replies is still not proven by
one live smoke test -- spot-check the first classified batch by hand.

Usage:
    python feedback.py --db ../sender/sender_state.db classify
    python feedback.py --db ../sender/sender_state.db report
    python feedback.py --db ../sender/sender_state.db export-few-shot --crawl-dir ../crawler/output --out few_shot.json
"""
import argparse
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

from google import genai
from google.genai import types

try:
    from dotenv import load_dotenv
    load_dotenv()  # optional -- picks up a local .env with GEMINI_API_KEY if python-dotenv is installed
except ImportError:
    pass  # fine without it -- just means you set the real env var yourself

sys.path.insert(0, str(Path(__file__).parent.parent / "sender"))
import db as db_module  # noqa: E402 -- real cross-module reuse, not a stub

log = logging.getLogger("feedback")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                     handlers=[logging.StreamHandler(sys.stdout)])

DEFAULT_MODEL = "gemini-3.6-flash"
MIN_SAMPLE_SIZE = 200  # per the original design: don't trust a rate before ~200 sends per arm

DEFAULT_THRESHOLDS = {
    "bounce_rate_max": 0.02,
    "reply_rate_min": 0.05,
    "reply_rate_max": 0.12,
    "positive_rate_min": 0.01,
    "positive_rate_max": 0.03,
}


# ---------------------------------------------------------------------------
# Sentiment classification
# ---------------------------------------------------------------------------

SENTIMENT_PROMPT_TEMPLATE = """A cold sales email got this reply. Classify the sender's sentiment.

REPLY TEXT:
{reply_text}

Return ONLY a JSON object (no markdown, no commentary):
{{"sentiment": "positive", "confidence": 0, "reasoning": ""}}

"sentiment": "positive" (genuine interest, wants to learn more, asks a real question), "negative" (not interested, explicitly declining), or "neutral" (ambiguous, wrong person redirect, unclear).
"confidence": 1-10.
"reasoning": one sentence.
"""


def build_sentiment_prompt(reply_text: str) -> str:
    return SENTIMENT_PROMPT_TEMPLATE.format(reply_text=reply_text)


RETRYABLE_STATUS_CODES = {429, 503}  # rate limit / transient overload, hit
# live for real during this project (a genuine 503 from Google's side)
MAX_RETRIES = 2
RETRY_BACKOFF_SEC = 5


def call_gemini_sentiment(prompt: str, api_key: str, model: str = DEFAULT_MODEL) -> dict:
    """Never raises. Uses Gemini's native JSON mode, same as every other
    stage in this project -- no prefill hack needed. max_output_tokens is
    4096, not the tiny sentiment-response size (3 fields) alone --
    gemini-3.6-flash is a reasoning model that spends part of this same
    budget on invisible "thinking" tokens before writing the visible JSON
    (real bug hit and fixed live in analyze.py first: too small a budget
    silently truncates the response mid-object). Retries transient
    429/503 errors with a fixed backoff before giving up."""
    if not api_key:
        return {"_api_error": True, "_error_detail": "no API key provided"}
    last_error = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            client = genai.Client(api_key=api_key)
            response = client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    max_output_tokens=4096,
                ),
            )
            cleaned = re.sub(r"^```(?:json)?\s*", "", response.text.strip())
            cleaned = re.sub(r"\s*```$", "", cleaned)
            return json.loads(cleaned)
        except genai.errors.APIError as e:
            last_error = e
            if e.code not in RETRYABLE_STATUS_CODES or attempt == MAX_RETRIES:
                break
            log.warning(f"Gemini call failed ({e.code} {e.status}), retrying "
                        f"in {RETRY_BACKOFF_SEC}s (attempt {attempt + 1}/{MAX_RETRIES})")
            time.sleep(RETRY_BACKOFF_SEC)
        except Exception as e:
            last_error = e
            break
    return {"_api_error": True, "_error_detail": str(last_error)}


def classify_reply_sentiment(reply_text: str, api_key: str, model: str = DEFAULT_MODEL) -> dict:
    prompt = build_sentiment_prompt(reply_text)
    return call_gemini_sentiment(prompt, api_key=api_key, model=model)


def run_sentiment_batch(conn, api_key: str, model: str = DEFAULT_MODEL) -> dict:
    unclassified = db_module.get_unclassified_replies(conn)
    summary = {"classified": 0, "api_errors": 0}
    for reply in unclassified:
        result = classify_reply_sentiment(reply["body_text"], api_key=api_key, model=model)
        if result.get("_api_error"):
            summary["api_errors"] += 1
            log.warning(f"[{reply['email']}] sentiment classification failed: {result.get('_error_detail')}")
            continue
        db_module.update_reply_sentiment(conn, reply["id"], result.get("sentiment", "neutral"),
                                          confidence=result.get("confidence"))
        summary["classified"] += 1
        log.info(f"[{reply['email']}] classified as {result.get('sentiment')}")
    return summary


# ---------------------------------------------------------------------------
# Variant metrics
# ---------------------------------------------------------------------------

def variant_key(subject: str) -> str:
    """All 4 steps of one sequence thread under the same base subject
    ('Re: quick question' -> 'quick question') -- this identifies the
    template/variant a lead was sent, not the individual message."""
    cleaned = re.sub(r"^(re:\s*)+", "", subject.strip(), flags=re.I)
    return cleaned.strip().lower()


def compute_variant_metrics(conn) -> dict:
    """One row per distinct subject-line variant. 'sent' counts step-1
    sends only (that's how many distinct leads got this variant); bounces/
    replies/positives are counted against the same lead cohort."""
    step1_sends = conn.execute(
        "SELECT email, subject FROM send_log WHERE step = 1 AND status = 'sent'"
    ).fetchall()

    variants = {}
    for row in step1_sends:
        key = variant_key(row["subject"] or "")
        v = variants.setdefault(key, {"sent": 0, "bounced": 0, "replied": 0, "positive": 0})
        v["sent"] += 1

        lead = db_module.get_lead(conn, row["email"])
        if lead and lead["status"] == "bounced":
            v["bounced"] += 1
        if lead and lead["status"] in ("replied",):
            v["replied"] += 1

    positive_replies = db_module.get_replies_by_sentiment(conn, "positive")
    for reply in positive_replies:
        lead_row = conn.execute("SELECT subject FROM send_log WHERE email = ? AND step = 1 LIMIT 1",
                                 (reply["email"],)).fetchone()
        if not lead_row:
            continue
        key = variant_key(lead_row["subject"] or "")
        if key in variants:
            variants[key]["positive"] += 1

    for key, v in variants.items():
        sent = v["sent"] or 1
        v["bounce_rate"] = round(v["bounced"] / sent, 4)
        v["reply_rate"] = round(v["replied"] / sent, 4)
        v["positive_rate"] = round(v["positive"] / sent, 4)
        v["sufficient_sample"] = v["sent"] >= MIN_SAMPLE_SIZE

    return variants


def check_health(metrics: dict, thresholds: dict = None) -> list:
    thresholds = thresholds or DEFAULT_THRESHOLDS
    warnings = []

    if not metrics.get("sufficient_sample", False):
        warnings.append(f"sample size ({metrics.get('sent', 0)}) below the {MIN_SAMPLE_SIZE} minimum -- "
                         f"rates below aren't trustworthy yet, keep sending before drawing conclusions")
        return warnings  # don't also flag rates that aren't statistically meaningful yet

    if metrics.get("bounce_rate", 0) > thresholds["bounce_rate_max"]:
        warnings.append(f"bounce rate {metrics['bounce_rate']:.1%} exceeds {thresholds['bounce_rate_max']:.1%} -- "
                         f"stop sending this variant and check list quality / mailbox health")
    if metrics.get("reply_rate", 0) < thresholds["reply_rate_min"]:
        warnings.append(f"reply rate {metrics['reply_rate']:.1%} below healthy floor {thresholds['reply_rate_min']:.1%}")
    if metrics.get("positive_rate", 0) < thresholds["positive_rate_min"]:
        warnings.append(f"positive-reply rate {metrics['positive_rate']:.1%} below healthy floor "
                         f"{thresholds['positive_rate_min']:.1%}")

    return warnings


# ---------------------------------------------------------------------------
# Few-shot example builder -- the actual compounding loop
# ---------------------------------------------------------------------------

def build_few_shot_examples(conn, crawl_dir: str, limit: int = 5) -> list:
    """Leads who replied POSITIVELY -> their winning subject + the verified
    facts that grounded that email -> formatted for draft.py to reference
    when drafting the next batch. This is what makes the pipeline actually
    improve over time instead of guessing fresh every run."""
    crawl_dir = Path(crawl_dir)
    positive_replies = db_module.get_replies_by_sentiment(conn, "positive")

    examples = []
    for reply in positive_replies:
        if len(examples) >= limit:
            break
        lead = db_module.get_lead(conn, reply["email"])
        if not lead:
            continue
        domain = lead["domain"]

        send_row = conn.execute(
            "SELECT subject FROM send_log WHERE email = ? AND step = 1 AND status = 'sent' LIMIT 1",
            (reply["email"],),
        ).fetchone()
        if not send_row:
            continue

        analysis_path = crawl_dir / domain / "analysis.json"
        if not analysis_path.exists():
            continue
        analysis = json.loads(analysis_path.read_text(encoding="utf-8"))

        grounding_parts = [h.get("quote", "") for h in (analysis.get("hooks") or [])]
        examples.append({
            "domain": domain,
            "company_name": analysis.get("company_name", domain),
            "subject": variant_key(send_row["subject"] or ""),
            "grounding": "; ".join(grounding_parts),
            "reply_text": reply["body_text"],
        })

    return examples


def format_few_shot_block(examples: list) -> str:
    if not examples:
        return ""
    lines = ["Past emails that got a genuinely positive reply -- match this style/structure where relevant, "
             "but still ground every claim in THIS lead's own verified facts, never reuse these facts for a different company:"]
    for ex in examples:
        lines.append(f"\n- Company: {ex['company_name']}")
        lines.append(f"  Grounded in: {ex['grounding']}")
        lines.append(f"  Subject that worked: \"{ex['subject']}\"")
        lines.append(f"  They replied: \"{ex['reply_text'][:150]}\"")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_classify(args):
    conn = db_module.get_connection(args.db)
    db_module.init_db(conn)
    summary = run_sentiment_batch(conn, api_key=args.api_key, model=args.model)
    print(f"Classified: {summary['classified']}  API errors: {summary['api_errors']}")


def cmd_report(args):
    conn = db_module.get_connection(args.db)
    db_module.init_db(conn)
    metrics = compute_variant_metrics(conn)
    if not metrics:
        print("No step-1 sends recorded yet.")
        return
    for key, v in metrics.items():
        print(f"\n=== Variant: \"{key}\" ===")
        print(f"  sent={v['sent']} bounced={v['bounced']} replied={v['replied']} positive={v['positive']}")
        print(f"  bounce_rate={v['bounce_rate']:.1%} reply_rate={v['reply_rate']:.1%} positive_rate={v['positive_rate']:.1%}")
        for w in check_health(v):
            print(f"  ⚠ {w}")


def cmd_export_few_shot(args):
    conn = db_module.get_connection(args.db)
    db_module.init_db(conn)
    examples = build_few_shot_examples(conn, args.crawl_dir, limit=args.limit)
    Path(args.out).write_text(json.dumps(examples, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Exported {len(examples)} few-shot example(s) to {args.out}")
    print(f"\nNext: python ../draft/draft.py --crawl-dir {args.crawl_dir} --offer-config offer_config.json "
          f"--few-shot {args.out}")


def main():
    ap = argparse.ArgumentParser(description="Stage 10: Feedback loop.")
    ap.add_argument("--db", default="../sender/sender_state.db")
    ap.add_argument("--api-key", default=os.environ.get("GEMINI_API_KEY"))
    ap.add_argument("--model", default=DEFAULT_MODEL)
    sub = ap.add_subparsers(dest="command", required=True)

    sub.add_parser("classify", help="classify unclassified replies as positive/negative/neutral")
    sub.add_parser("report", help="print per-variant metrics + health warnings")

    export_ap = sub.add_parser("export-few-shot", help="export positive-reply examples for draft.py")
    export_ap.add_argument("--crawl-dir", default="../crawler/output")
    export_ap.add_argument("--out", default="few_shot.json")
    export_ap.add_argument("--limit", type=int, default=5)

    args = ap.parse_args()

    if args.command == "classify":
        if not args.api_key:
            raise SystemExit("No API key. Set GEMINI_API_KEY or pass --api-key.")
        cmd_classify(args)
    elif args.command == "report":
        cmd_report(args)
    elif args.command == "export-few-shot":
        cmd_export_few_shot(args)


if __name__ == "__main__":
    main()
