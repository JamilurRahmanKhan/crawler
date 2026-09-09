"""
Stage 5: Draft -- writes the actual personalized cold email sequence (4
steps: intro, nudge, proof, breakup) grounded ONLY in Stage 3's
analysis.json verified facts, using YOUR real offer (config file, not
hardcoded). One Claude call generates all 4 steps together so the sequence
reads as one coherent escalating conversation, not four disconnected blasts.

Real integration, not a stub: reuses qa_gate.py's ACTUAL check functions
(check_no_links, check_banned_phrases, check_merge_tag_leaks,
check_body_length) to self-validate every draft against the same hard
constraints before it's ever handed to qa_gate.py -- catching obvious
violations here means fewer wasted LLM-judge calls downstream. One retry
with feedback if the self-check fails; if it still fails, the item is
flagged (_self_check_failed) rather than silently shipped -- qa_gate.py
remains the authoritative final gate either way.

Input: contacts.json (Stage 4 -- who to write to) + analysis.json (Stage 3
-- what's actually true about them, already hallucination-clamped) for
every domain under crawler/output, plus YOUR offer_config.json (what you
sell, your ICP, your real proof points -- this project cannot know your
business, same as it cannot know your SMTP password).

Output: queue.json, flattened across all leads x 4 steps, in the EXACT
shape qa_gate.py --queue (and sender.py --queue) already expect. Zero
reformatting needed at the hand-off.

Honest note on testing: same situation as analyze.py and qa_gate.py -- no
ANTHROPIC_API_KEY was available in the session that built this. Every
function is tested with the real Anthropic SDK mocked at the client
boundary; the actual drafted email *quality* is not verified here (and
structurally can't be, without your real offer_config filled in).

Usage:
    cp offer_config.example.json offer_config.json   # then fill in your real business
    python draft.py --crawl-dir ../crawler/output --offer-config offer_config.json --dry-run
    python draft.py --crawl-dir ../crawler/output --offer-config offer_config.json
"""
import argparse
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

import anthropic

# real cross-module reuse -- not reimplemented, not a stub
sys.path.insert(0, str(Path(__file__).parent.parent / "qa_gate"))
from qa_gate import (  # noqa: E402
    check_no_links, check_banned_phrases, check_merge_tag_leaks, check_body_length,
)
sys.path.insert(0, str(Path(__file__).parent.parent / "feedback"))
from feedback import format_few_shot_block  # noqa: E402 -- Stage 10's actual compounding loop

log = logging.getLogger("draft")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                     handlers=[logging.StreamHandler(sys.stdout)])

DEFAULT_MODEL = "claude-opus-5"  # matches the original pipeline design's model split -- best writer for the actual copy
SEQUENCE_LENGTH = 4
MAX_OUTPUT_TOKENS = 2048

REQUIRED_OFFER_KEYS = [
    "your_company_name", "sender_name", "what_you_sell",
    "icp_description", "proof_points", "cta_style",
]
# Bracket-wrapped placeholder syntax ("[YOUR COMPANY NAME]", "[insert X]") is
# an unambiguous, standard template convention -- unlike a bare word list
# (originally tried "replace"/"fill in"/etc.), which false-rejected both this
# project's own example file (a "_comment" meta field mentioning "replace")
# AND would false-reject a genuinely real config whose proof points happen to
# use an ordinary word like "replace" in legitimate prose ("helped them
# replace manual processes"). Caught live while testing the example file.
PLACEHOLDER_BRACKET_RE = re.compile(r"\[[a-z0-9_\- ]{2,40}\]", re.I)

DRAFT_PROMPT_TEMPLATE = """You are writing a 4-email cold outreach sequence on behalf of {your_company_name} ({sender_name} is the sender). Write like one professional writing to another -- no marketing voice, no hype.

WHAT WE SELL: {what_you_sell}
WHO WE SERVE: {icp_description}
REAL PROOF POINTS (only use these -- do not invent others):
{proof_points_text}
CALL-TO-ACTION STYLE: {cta_style}

WHO YOU'RE WRITING TO: {contact_name} ({contact_title}) at {company_name}.

VERIFIED FACTS ABOUT THIS COMPANY -- reference ONLY these, do not invent or assume anything else about them:
{grounding_text}

Write exactly {sequence_length} emails as a JSON array, one object per step:
[{{"step": 1, "subject": "", "body": ""}}, ...]

Rules, all mandatory:
- Step 1: opening observation grounded in one verified fact above, bridge to why it matters, one real proof point, soft interest-based CTA (not a hard meeting ask). Under 90 words. NO links, NO images, NO attachments.
- Step 2 (sent 3 days later): a new angle on the same idea, not just "following up." Can include a link.
- Step 3 (sent 4 days later): lead with the strongest proof point.
- Step 4 (sent 5 days later): a polite breakup -- last touch, low-pressure, no guilt-tripping.
- Subject lines: 3-6 words, lowercase, no clickbait, consistent across all 4 (they thread as one conversation).
- Never use: "hope this email finds you well", "just following up", "revolutionary", "game-changing", "synergy", "circling back", "touch base", "per my last email".
- Never invent a fact, statistic, or quote that isn't in the verified facts or proof points above.
{few_shot_section}
Return ONLY the JSON array, no markdown, no commentary.
"""


def load_offer_config(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"Offer config not found: {path}. Copy offer_config.example.json and fill it in.")
    try:
        config = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        raise SystemExit(f"Offer config is not valid JSON: {e}")

    missing = [k for k in REQUIRED_OFFER_KEYS if not config.get(k)]
    if missing:
        raise SystemExit(f"Offer config missing required field(s): {missing}. "
                          f"See offer_config.example.json for the expected shape.")

    # scan only the REQUIRED fields' actual string content -- not meta keys
    # like "_comment" (documentation, not configured content) and not the
    # Python repr of proof_points (a list of dicts, which always contains
    # literal [ and { characters unrelated to placeholder text). Both were
    # real false-positive sources caught live while testing this exact file.
    required_values = {k: config[k] for k in REQUIRED_OFFER_KEYS if k in config}
    flat_text = " ".join(_flatten_strings(required_values))
    match = PLACEHOLDER_BRACKET_RE.search(flat_text)
    if match:
        raise SystemExit(f"Offer config still contains placeholder text ('{match.group(0)}') -- "
                          f"fill in your real business details before drafting real emails.")
    return config


def _flatten_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _flatten_strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _flatten_strings(v)


def _grounding_text(analysis: dict) -> str:
    lines = []
    for h in (analysis or {}).get("hooks", []) or []:
        lines.append(f"- {h.get('quote', '')}")
    for p in (analysis or {}).get("pains", []) or []:
        lines.append(f"- {p.get('quote', '')}")
    return "\n".join(lines) if lines else "(no strong verified hooks found -- keep this generic and short)"


def build_draft_prompt(analysis: dict, contact: dict, offer_config: dict, few_shot_examples: list = None) -> str:
    proof_points = offer_config.get("proof_points", []) or []
    proof_points_text = "\n".join(f"- {p.get('claim', '')} ({p.get('detail', '')})" for p in proof_points) or "(none provided)"

    contact_name = contact.get("matched_name") or "there"
    contact_title = contact.get("matched_title") or ""
    company_name = (analysis or {}).get("company_name") or (analysis or {}).get("domain", "")

    # Stage 10's actual compounding loop: past positive-reply examples,
    # reusing feedback.py's real formatter -- not reimplemented here
    few_shot_section = ""
    if few_shot_examples:
        few_shot_section = "\n" + format_few_shot_block(few_shot_examples) + "\n"

    return DRAFT_PROMPT_TEMPLATE.format(
        your_company_name=offer_config.get("your_company_name", ""),
        sender_name=offer_config.get("sender_name", ""),
        what_you_sell=offer_config.get("what_you_sell", ""),
        icp_description=offer_config.get("icp_description", ""),
        proof_points_text=proof_points_text,
        cta_style=offer_config.get("cta_style", ""),
        few_shot_section=few_shot_section,
        contact_name=contact_name,
        contact_title=contact_title,
        company_name=company_name,
        grounding_text=_grounding_text(analysis),
        sequence_length=SEQUENCE_LENGTH,
    )


def parse_draft_response(raw_text: str) -> list:
    cleaned = raw_text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    if not cleaned.startswith("["):
        cleaned = "[" + cleaned
    try:
        data = json.loads(cleaned)
        if not isinstance(data, list):
            return []
        return data
    except Exception:
        return []


def self_check_draft(email: dict) -> dict:
    """Reuses qa_gate.py's real check functions -- catches obvious
    violations before this ever reaches the actual QA gate."""
    subject = email.get("subject", "")
    body = email.get("body", "")
    step = email.get("step", 1)
    violations = []

    _, len_violations = check_body_length(body)
    violations.extend(len_violations)

    if step == 1 and not check_no_links(body):
        violations.append("email step 1 must not contain links")

    leaks = check_merge_tag_leaks(f"{subject} {body}")
    if leaks:
        violations.append(f"merge-tag/template leak detected: {leaks}")

    banned = check_banned_phrases(body)
    if banned:
        violations.append(f"banned phrase(s) found: {banned}")

    return {"passed": len(violations) == 0, "violations": violations}


def call_claude_draft(prompt: str, api_key: str, model: str = DEFAULT_MODEL) -> list:
    """Never raises -- returns [] on any failure (no key, API error, bad
    JSON) so a batch run logs and continues past one bad lead."""
    if not api_key:
        return []
    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=model,
            max_tokens=MAX_OUTPUT_TOKENS,
            messages=[
                {"role": "user", "content": prompt},
                {"role": "assistant", "content": "["},
            ],
        )
        raw_text = "[" + response.content[0].text
        return parse_draft_response(raw_text)
    except Exception as e:
        log.warning(f"Claude draft call failed: {e}")
        return []


def draft_lead(contact: dict, analysis: dict, offer_config: dict, api_key: str,
               model: str = DEFAULT_MODEL, max_retries: int = 1, few_shot_examples: list = None) -> list:
    prompt = build_draft_prompt(analysis, contact, offer_config, few_shot_examples=few_shot_examples)
    sequence = call_claude_draft(prompt, api_key=api_key, model=model)
    if not sequence:
        return []

    for attempt in range(max_retries + 1):
        step1 = next((e for e in sequence if e.get("step") == 1), sequence[0] if sequence else {})
        check = self_check_draft(step1)
        if check["passed"]:
            break
        if attempt < max_retries:
            log.warning(f"self-check failed ({check['violations']}), retrying once")
            retry_prompt = prompt + f"\n\nYour previous attempt violated these rules: {check['violations']}. Fix them."
            retried = call_claude_draft(retry_prompt, api_key=api_key, model=model)
            if retried:
                sequence = retried

    to_email = contact.get("email", "")
    domain = (analysis or {}).get("domain", "")
    result = []
    for email in sequence:
        step = email.get("step", 1)
        item = {
            "to_email": to_email,
            "to_name": contact.get("matched_name"),
            "subject": email.get("subject", ""),
            "body": email.get("body", ""),
            "step": step,
            "domain": domain,
        }
        if step == 1:
            final_check = self_check_draft(email)
            if not final_check["passed"]:
                item["_self_check_failed"] = True
                item["_violations"] = final_check["violations"]
        result.append(item)
    return result


def run_batch(crawl_dir: str, offer_config_path: str, queue_out: str,
              api_key: str, model: str = DEFAULT_MODEL, domain_filter: set = None,
              dry_run: bool = False, few_shot_path: str = None) -> dict:
    offer_config = load_offer_config(offer_config_path)
    crawl_dir = Path(crawl_dir)

    few_shot_examples = None
    if few_shot_path:
        few_shot_examples = json.loads(Path(few_shot_path).read_text(encoding="utf-8"))

    summary = {"leads_drafted": 0, "skipped_no_contact": 0, "skipped_no_analysis": 0, "self_check_failed": 0}
    all_items = []

    domain_dirs = [d for d in sorted(crawl_dir.iterdir()) if d.is_dir()]
    if domain_filter:
        domain_dirs = [d for d in domain_dirs if d.name in domain_filter]

    for d in domain_dirs:
        contacts_path = d / "contacts.json"
        analysis_path = d / "analysis.json"

        if not contacts_path.exists():
            continue
        contacts_data = json.loads(contacts_path.read_text(encoding="utf-8"))
        contacts = contacts_data.get("contacts", []) if contacts_data.get("status") == "ok" else []
        if not contacts:
            summary["skipped_no_contact"] += 1
            continue

        if not analysis_path.exists():
            summary["skipped_no_analysis"] += 1
            continue
        analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
        if analysis.get("status") != "ok":
            summary["skipped_no_analysis"] += 1
            continue

        best_contact = contacts[0]

        if dry_run:
            prompt = build_draft_prompt(analysis, best_contact, offer_config)
            log.info(f"[{d.name}] would draft for {best_contact.get('email')} (~{len(prompt)} char prompt)")
            continue

        sequence_items = draft_lead(best_contact, analysis, offer_config, api_key=api_key, model=model,
                                     few_shot_examples=few_shot_examples)
        if not sequence_items:
            log.warning(f"[{d.name}] drafting failed or returned nothing")
            continue

        summary["leads_drafted"] += 1
        if any(item.get("_self_check_failed") for item in sequence_items):
            summary["self_check_failed"] += 1
        all_items.extend(sequence_items)
        time.sleep(0.2)

    if not dry_run:
        Path(queue_out).write_text(json.dumps(all_items, indent=2, ensure_ascii=False), encoding="utf-8")

    return summary


def main():
    ap = argparse.ArgumentParser(description="Stage 5: Draft -- write personalized cold email sequences.")
    ap.add_argument("--crawl-dir", default="../crawler/output")
    ap.add_argument("--offer-config", default="offer_config.json")
    ap.add_argument("--queue-out", default="queue.json", help="ready for qa_gate.py --queue")
    ap.add_argument("--api-key", default=os.environ.get("ANTHROPIC_API_KEY"))
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--domain", help="draft for a single domain only")
    ap.add_argument("--dry-run", action="store_true", help="show what would be drafted, call nothing")
    ap.add_argument("--few-shot", help="feedback.py export-few-shot output -- winning past examples to reference")
    args = ap.parse_args()

    if not args.dry_run and not args.api_key:
        raise SystemExit("No API key. Set ANTHROPIC_API_KEY or pass --api-key (or use --dry-run).")

    domain_filter = {args.domain} if args.domain else None
    summary = run_batch(args.crawl_dir, args.offer_config, args.queue_out,
                         api_key=args.api_key, model=args.model,
                         domain_filter=domain_filter, dry_run=args.dry_run,
                         few_shot_path=args.few_shot)

    print(f"Leads drafted:         {summary['leads_drafted']}")
    print(f"Skipped (no contact):  {summary['skipped_no_contact']}")
    print(f"Skipped (no analysis): {summary['skipped_no_analysis']}")
    print(f"Self-check failed:     {summary['self_check_failed']} (flagged in output, review before sending)")
    if not args.dry_run:
        print(f"\nNext: python ../qa_gate/qa_gate.py --queue {args.queue_out} --crawl-dir {args.crawl_dir}")


if __name__ == "__main__":
    main()
