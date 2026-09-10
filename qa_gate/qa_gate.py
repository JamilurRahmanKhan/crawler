"""
Stage 6: QA gate -- validates a drafted email before it's allowed anywhere
near sender.py. Two tiers, per the original design:

  1. Automated (free, deterministic, runs first): word count, no links in
     email 1, no images/attachments, merge-tag/template leaks ({{...}},
     literal "undefined"/"NAME"/"null", AI self-disclosure phrases), banned
     cliche phrases, reading grade, and -- the real integration -- grounding:
     does the draft actually reference a verified fact from Stage 3's
     analysis.json for this domain, not just generic filler. An automated
     failure short-circuits before ever spending an LLM call.

  2. LLM judge (a second, separate Claude call): scores grounding,
     specificity, peer-tone, and "would a busy founder reply" 1-10. Needs
     grounding >= 8 AND overall >= 7 to pass.

Human review sampling: the first 50 items ever processed (persisted across
runs in a small state file) are flagged for 100% human review during
calibration; after that, anything borderline (overall <= 7) is always
flagged, plus a random ~10% ongoing sample. Sampling is INFORMATIONAL --
logged to the review CSV for calibration -- it does not block an item that
otherwise passed from reaching the send queue. Only a genuine automated or
judge failure does that. (Blocking on every sampled item would mean queue
throughput depends on a coin flip, which isn't a real gate, just noise.)

Input contract: same shape sender.py's --queue already expects
({to_email, subject, body, step}, optionally "domain" -- derived from
to_email's domain if absent) -- decoupled from however the draft got
written (Stage 5, or a manual batch today), same pattern that let
sender.py get built before Draft existed.

Output: queue_passed.json is byte-for-byte usable as sender.py's --queue
input, zero reformatting. manual_review.csv is the audit trail -- every
failure AND every review-sampled pass, with reasons, nothing silently
discarded.

Uses Google's Gemini API (google-genai SDK), same as analyze.py, at $0
budget. The LLM judge call is fully tested with the real google-genai
client mocked at the client boundary, and was ALSO verified live against
the real Gemini API with a real free-tier key -- the judge's actual
scoring *quality* on a large, varied batch is still not proven by one
live smoke test; spot-check the first batch by hand regardless.

Usage:
    python qa_gate.py --queue queue.json --crawl-dir ../crawler/output --dry-run
    python qa_gate.py --queue queue.json --crawl-dir ../crawler/output
"""
import argparse
import csv
import json
import logging
import os
import random
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

log = logging.getLogger("qa_gate")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                     handlers=[logging.StreamHandler(sys.stdout)])

DEFAULT_MODEL = "gemini-3.6-flash"
MAX_BODY_WORDS = 90
MAX_READING_GRADE = 8
GROUNDING_THRESHOLD = 8
OVERALL_THRESHOLD = 7
FIRST_N_ALWAYS_REVIEWED = 50
ONGOING_SAMPLE_RATE = 0.10
BORDERLINE_OVERALL_MAX = 7

DEFAULT_BANNED_PHRASES = [
    "hope this email finds you well",
    "just following up",
    "revolutionary",
    "game-changing",
    "synergy",
    "circling back",
    "touch base",
    "per my last email",
]

MERGE_TAG_PATTERNS = [
    ("double_curly", re.compile(r"\{\{[^}]*\}\}")),
    ("double_bracket", re.compile(r"\[\[[^\]]*\]\]")),
    ("literal_undefined", re.compile(r"\bundefined\b", re.I)),
    ("literal_null", re.compile(r"\bnull\b", re.I)),
    ("literal_name_token", re.compile(r"\bNAME\b")),  # case-sensitive on purpose
    ("ai_self_disclosure", re.compile(r"\bas an ai\b", re.I)),
    ("ai_no_access", re.compile(r"i don['’]t have access", re.I)),
]

_STOPWORDS = {"the", "and", "that", "this", "with", "from", "your", "have",
              "been", "were", "they", "their", "about", "which", "would",
              "could", "should", "there"}


# ---------------------------------------------------------------------------
# Automated checks
# ---------------------------------------------------------------------------

def count_words(text: str) -> int:
    return len(text.split())


def check_body_length(body: str, max_words: int = MAX_BODY_WORDS) -> tuple:
    n = count_words(body)
    if n > max_words:
        return False, [f"body exceeds {max_words} words ({n} words)"]
    return True, []


def check_no_links(body: str) -> bool:
    return re.search(r"https?://|www\.", body, re.I) is None


def check_no_images_or_attachments(body: str) -> bool:
    if re.search(r"!\[[^\]]*\]\([^)]*\)", body):
        return False
    if re.search(r"<img\b", body, re.I):
        return False
    return True


def check_merge_tag_leaks(text: str) -> list:
    found = []
    for label, pattern in MERGE_TAG_PATTERNS:
        m = pattern.search(text)
        if m:
            found.append(f"{label}: '{m.group(0)}'")
    return found


def check_banned_phrases(text: str, banned_list: list = None) -> list:
    banned_list = banned_list if banned_list is not None else DEFAULT_BANNED_PHRASES
    text_lower = text.lower()
    return [p for p in banned_list if p.lower() in text_lower]


def count_syllables(word: str) -> int:
    word = re.sub(r"[^a-z]", "", word.lower())
    if not word:
        return 0
    vowels = "aeiouy"
    count = 0
    prev_was_vowel = False
    for ch in word:
        is_vowel = ch in vowels
        if is_vowel and not prev_was_vowel:
            count += 1
        prev_was_vowel = is_vowel
    if word.endswith("e") and count > 1:
        count -= 1
    return max(count, 1)


def estimate_reading_grade(text: str) -> float:
    """Flesch-Kincaid grade level, pure-python (no external dependency)."""
    text = text.strip()
    if not text:
        return 0
    sentences = [s for s in re.split(r"[.!?]+", text) if s.strip()]
    words = re.findall(r"[A-Za-z']+", text)
    if not sentences or not words:
        return 0
    syllables = sum(count_syllables(w) for w in words)
    grade = 0.39 * (len(words) / len(sentences)) + 11.8 * (syllables / len(words)) - 15.59
    return round(max(grade, 0), 1)


def _keywords(text: str) -> set:
    words = re.findall(r"[a-zA-Z']+", text.lower())
    return {w for w in words if len(w) >= 5 and w not in _STOPWORDS}


def check_grounding_keywords(body: str, analysis: dict) -> bool:
    """Cheap automated pre-check: does the body reference ANY verified hook
    or pain from Stage 3's analysis.json for this domain -- catches the
    worst "totally generic, ignored the real data" offenders for free,
    before spending an LLM judge call. The judge call does the nuanced
    version; this just catches the obvious misses."""
    if not analysis:
        return False
    candidates = []
    for h in analysis.get("hooks", []) or []:
        candidates.append(f"{h.get('quote', '')} {h.get('hook', '')}")
    for p in analysis.get("pains", []) or []:
        candidates.append(f"{p.get('quote', '')} {p.get('pain', '')}")
    if not candidates:
        return False
    body_lower = body.lower()
    for c in candidates:
        c_norm = c.lower().strip()
        if c_norm and c_norm in body_lower:
            return True
        if any(kw in body_lower for kw in _keywords(c)):
            return True
    return False


def run_automated_checks(item: dict, analysis: dict = None) -> dict:
    subject = item.get("subject", "")
    body = item.get("body", "")
    step = item.get("step", 1)
    violations = []

    _, len_violations = check_body_length(body)
    violations.extend(len_violations)

    if step == 1 and not check_no_links(body):
        violations.append("email step 1 must not contain links")

    if not check_no_images_or_attachments(body):
        violations.append("body contains an image or attachment reference")

    leaks = check_merge_tag_leaks(f"{subject} {body}")
    if leaks:
        violations.append(f"merge-tag/template leak detected: {leaks}")

    banned = check_banned_phrases(body)
    if banned:
        violations.append(f"banned phrase(s) found: {banned}")

    grade = estimate_reading_grade(body)
    if grade > MAX_READING_GRADE:
        violations.append(f"reading grade too high ({grade}, max {MAX_READING_GRADE})")

    # Grounding is only required on step 1 (the personalized cold open).
    # Verified live via a real Draft -> QA gate integration test: a genuine
    # 4-step sequence's follow-up/proof-point/breakup emails don't
    # necessarily re-cite the SAME verified hook step 1 used -- the original
    # sequence design has step 3 lead with a proof point (the sender's own
    # claim, not something to verify against the target's site) and step 4
    # be a low-content breakup message. Enforcing grounding on every step
    # would reject legitimate follow-ups wholesale.
    #
    # Also only enforce when there's actually something groundable -- a
    # missing/unavailable analysis.json is a data-availability gap, not a
    # quality problem with this specific draft; don't penalize for it.
    has_groundable_data = bool(analysis and (analysis.get("hooks") or analysis.get("pains")))
    if step == 1 and has_groundable_data and not check_grounding_keywords(body, analysis):
        violations.append("no verified detail from analysis.json referenced in body")

    return {"passed": len(violations) == 0, "violations": violations}


# ---------------------------------------------------------------------------
# LLM judge
# ---------------------------------------------------------------------------

JUDGE_PROMPT_TEMPLATE = """You are a strict quality judge for a cold sales email. Score it honestly -- this email will only send if it's genuinely good, not just passable.

EMAIL SUBJECT: {subject}
EMAIL BODY:
{body}

VERIFIED FACTS AVAILABLE ABOUT THIS COMPANY (from their own website):
{grounding_source}

Score each 1-10 and return ONLY a JSON object (no markdown, no commentary):
{{
  "grounding": 0,
  "specificity": 0,
  "peer_tone": 0,
  "would_reply": 0,
  "overall": 0,
  "feedback": ""
}}

"grounding": does the email's claims trace back to the verified facts above (10) or could it have been sent to any company (1)?
"specificity": how specific/personal is the hook, vs. generic industry talk?
"peer_tone": does this read like one professional writing to another, not a marketing blast?
"would_reply": would a busy founder actually reply to this?
"overall": your holistic judgment.
"feedback": one sentence on what would make this better, if anything.
"""


def build_judge_prompt(item: dict, analysis: dict = None) -> str:
    grounding_source = "(none available)"
    if analysis:
        parts = []
        for h in analysis.get("hooks", []) or []:
            parts.append(f"- {h.get('quote', '')}")
        for p in analysis.get("pains", []) or []:
            parts.append(f"- {p.get('quote', '')}")
        if parts:
            grounding_source = "\n".join(parts)
    return JUDGE_PROMPT_TEMPLATE.format(
        subject=item.get("subject", ""), body=item.get("body", ""), grounding_source=grounding_source,
    )


RETRYABLE_STATUS_CODES = {429, 503}  # rate limit / transient overload, hit
# live for real during this project (a genuine 503 from Google's side)
MAX_RETRIES = 2
RETRY_BACKOFF_SEC = 5


def call_gemini_judge(prompt: str, api_key: str, model: str = DEFAULT_MODEL) -> dict:
    """Never raises. Uses Gemini's native JSON mode, same as analyze.py's
    extraction call -- no prefill hack needed. max_output_tokens is 4096,
    not the tiny judge-response size (6 fields) alone -- gemini-3.6-flash
    is a reasoning model that spends part of this same budget on invisible
    "thinking" tokens before writing the visible JSON (real bug hit and
    fixed live in analyze.py first: too small a budget silently truncates
    the response mid-object). Retries transient 429/503 errors with a
    fixed backoff before giving up."""
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


def judge_email(item: dict, analysis: dict, api_key: str, model: str = DEFAULT_MODEL) -> dict:
    prompt = build_judge_prompt(item, analysis)
    return call_gemini_judge(prompt, api_key=api_key, model=model)


def should_pass(scores: dict) -> bool:
    if scores.get("_api_error"):
        return False
    return scores.get("grounding", 0) >= GROUNDING_THRESHOLD and scores.get("overall", 0) >= OVERALL_THRESHOLD


def should_sample_for_human_review(reviewed_so_far: int, scores: dict) -> bool:
    if reviewed_so_far < FIRST_N_ALWAYS_REVIEWED:
        return True
    if (scores or {}).get("overall", 0) <= BORDERLINE_OVERALL_MAX:
        return True
    return random.random() < ONGOING_SAMPLE_RATE


# ---------------------------------------------------------------------------
# Review-state persistence (stateful across batch runs)
# ---------------------------------------------------------------------------

def load_review_state(state_path: str) -> int:
    p = Path(state_path)
    if not p.exists():
        return 0
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("reviewed_so_far", 0)
    except Exception:
        return 0


def save_review_state(state_path: str, count: int):
    Path(state_path).write_text(json.dumps({"reviewed_so_far": count}), encoding="utf-8")


# ---------------------------------------------------------------------------
# Judge-feedback redraft loop (opt-in via --offer-config) -- one automatic
# retry using the judge's own specific rejection reason, before an item
# ever reaches manual review. draft.py is imported LAZILY (inside the
# function, not at module top) so qa_gate.py stays importable/usable on
# its own even where draft.py isn't present -- this project's own README
# for this stage states qa_gate "doesn't need Draft to exist to be
# complete"; the redraft is an enhancement, not a hard dependency.
# ---------------------------------------------------------------------------

def find_contact(crawl_dir: Path, domain: str, email: str) -> dict:
    contacts_path = Path(crawl_dir) / domain / "contacts.json"
    if contacts_path.exists():
        try:
            data = json.loads(contacts_path.read_text(encoding="utf-8"))
            for c in data.get("contacts", []) or []:
                if (c.get("email") or "").strip().lower() == email.strip().lower():
                    return c
        except Exception:
            pass
    return {"email": email, "matched_name": None, "matched_title": None}


def redraft_sequence_with_feedback(domain: str, email: str, analysis: dict, offer_config: dict,
                                    feedback: str, api_key: str, model: str, crawl_dir: Path) -> list:
    """Returns a freshly drafted 4-step sequence (draft_lead's own item
    shape, ready to re-check) or [] if draft.py isn't importable or the
    redraft call itself fails -- never raises, matches every other
    graceful-degrade pattern in this project."""
    try:
        sys.path.insert(0, str(Path(__file__).parent.parent / "draft"))
        import draft as draft_module
    except ImportError:
        return []
    contact = find_contact(crawl_dir, domain, email)
    return draft_module.draft_lead(contact, analysis, offer_config, api_key=api_key, model=model,
                                    previous_feedback=feedback)


def _evaluate_sequence(items: list, analysis: dict, api_key: str, model: str, reviewed_so_far: int):
    results = []
    for item in items:
        result = qa_check_item(item, analysis, api_key, model, reviewed_so_far)
        reviewed_so_far += 1
        results.append(result)
    return results, reviewed_so_far


# ---------------------------------------------------------------------------
# Per-item orchestration
# ---------------------------------------------------------------------------

def qa_check_item(item: dict, analysis: dict, api_key: str, model: str, reviewed_so_far: int) -> dict:
    automated = run_automated_checks(item, analysis)
    if not automated["passed"]:
        return {"verdict": "fail_automated", "violations": automated["violations"],
                "needs_human_review": True, "judge_scores": None}

    judge_scores = judge_email(item, analysis, api_key, model)
    if judge_scores.get("_api_error"):
        return {"verdict": "fail_judge_error", "violations": [judge_scores.get("_error_detail", "")],
                "needs_human_review": True, "judge_scores": judge_scores}

    passed_judge = should_pass(judge_scores)
    sampled = should_sample_for_human_review(reviewed_so_far, judge_scores)
    return {
        "verdict": "pass" if passed_judge else "fail_judge",
        "violations": [] if passed_judge else [judge_scores.get("feedback", "judge score below threshold")],
        "needs_human_review": sampled or not passed_judge,
        "judge_scores": judge_scores,
    }


# ---------------------------------------------------------------------------
# Batch runner
# ---------------------------------------------------------------------------

def run_batch(queue_path: str, crawl_dir: str, out_passed: str, out_review_csv: str,
              api_key: str, model: str = DEFAULT_MODEL, state_path: str = "qa_review_state.json",
              offer_config_path: str = None) -> dict:
    queue = json.loads(Path(queue_path).read_text(encoding="utf-8"))
    crawl_dir = Path(crawl_dir)
    reviewed_so_far = load_review_state(state_path)
    offer_config = (json.loads(Path(offer_config_path).read_text(encoding="utf-8"))
                    if offer_config_path else None)

    summary = {"total": len(queue), "passed": 0, "failed_automated": 0,
               "failed_judge": 0, "needs_review": 0, "redrafted": 0}
    passed_items = []
    review_rows = []

    # group by (domain, email) preserving first-appearance order -- a judge
    # failure on step 1 triggers ONE redraft of the whole 4-step sequence
    # (draft.py generates all 4 as one coherent thread; redrafting a single
    # step in isolation would break steps 2-4's continuity), not a redraft
    # per individual step.
    groups = {}
    for item in queue:
        domain = item.get("domain") or item.get("to_email", "").split("@", 1)[-1]
        key = (domain, (item.get("to_email") or "").strip().lower())
        groups.setdefault(key, []).append(item)

    for (domain, email), items in groups.items():
        analysis_path = crawl_dir / domain / "analysis.json"
        analysis = json.loads(analysis_path.read_text(encoding="utf-8")) if analysis_path.exists() else None

        results, reviewed_so_far = _evaluate_sequence(items, analysis, api_key, model, reviewed_so_far)

        step1_result = next((r for i, r in zip(items, results) if i.get("step") == 1), None)
        if offer_config and step1_result and step1_result["verdict"] == "fail_judge":
            feedback = (step1_result["judge_scores"] or {}).get("feedback", "")
            redrafted = redraft_sequence_with_feedback(
                domain, email, analysis, offer_config, feedback=feedback,
                api_key=api_key, model=model, crawl_dir=crawl_dir)
            if redrafted:
                new_results, reviewed_so_far = _evaluate_sequence(redrafted, analysis, api_key, model, reviewed_so_far)
                items, results = redrafted, new_results
                summary["redrafted"] += 1
                log.info(f"[{email}] step 1 failed judge ({feedback!r}) -- redrafted sequence")

        for item, result in zip(items, results):
            scores = result["judge_scores"] or {}
            row = {
                "to_email": item.get("to_email", ""), "domain": domain, "step": item.get("step", ""),
                "subject": item.get("subject", ""), "verdict": result["verdict"],
                "violations": "; ".join(result["violations"]),
                "judge_overall": scores.get("overall", ""),
            }

            if result["verdict"] == "pass":
                passed_items.append(item)
                summary["passed"] += 1
                if result["needs_human_review"]:
                    summary["needs_review"] += 1
                    review_rows.append(row)
            else:
                if result["verdict"] == "fail_automated":
                    summary["failed_automated"] += 1
                else:
                    summary["failed_judge"] += 1
                review_rows.append(row)

            log.info(f"[{row['to_email']}] {result['verdict']}"
                     + (f" -- {result['violations']}" if result["violations"] else ""))

    save_review_state(state_path, reviewed_so_far)

    Path(out_passed).write_text(json.dumps(passed_items, indent=2, ensure_ascii=False), encoding="utf-8")

    fieldnames = ["to_email", "domain", "step", "subject", "verdict", "violations", "judge_overall"]
    with open(out_review_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(review_rows)

    return summary


def main():
    ap = argparse.ArgumentParser(description="Stage 6: QA gate -- validate drafted emails before sending.")
    ap.add_argument("--queue", required=True, help="JSON queue of drafted emails (same shape sender.py --queue expects)")
    ap.add_argument("--crawl-dir", default="../crawler/output", help="for looking up each domain's analysis.json")
    ap.add_argument("--api-key", default=os.environ.get("GEMINI_API_KEY"))
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--passed-out", default="queue_passed.json", help="ready for sender.py --queue")
    ap.add_argument("--review-out", default="manual_review.csv", help="failures + review-sampled passes, audit trail")
    ap.add_argument("--state-path", default="qa_review_state.json", help="persists the review-sampling counter across runs")
    ap.add_argument("--offer-config", default=None,
                     help="enables one automatic redraft (via draft.py) when step 1 fails the judge, "
                          "using the judge's own feedback -- omit to disable (default)")
    args = ap.parse_args()

    if not args.api_key:
        raise SystemExit("No API key. Set GEMINI_API_KEY or pass --api-key.")

    summary = run_batch(args.queue, args.crawl_dir, args.passed_out, args.review_out,
                         api_key=args.api_key, model=args.model, state_path=args.state_path,
                         offer_config_path=args.offer_config)

    print(f"Total:            {summary['total']}")
    print(f"Passed:           {summary['passed']}  -> {args.passed_out}")
    print(f"Failed automated: {summary['failed_automated']}")
    print(f"Failed judge:     {summary['failed_judge']}")
    print(f"Redrafted:        {summary['redrafted']}")
    print(f"Flagged for human review (subset of passed + all failures): {summary['needs_review']}")
    print(f"Review audit trail: {args.review_out}")
    print(f"\nNext: python ../sender/sender.py --queue {args.passed_out} --dry-run")


if __name__ == "__main__":
    main()
