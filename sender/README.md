# Free Smartlead replacement (Stage 7 — sending engine)

Does Smartlead's three jobs at $0, using an SMTP/IMAP account you already
have (Gmail, Workspace, Outlook, anything):

1. **Warmup** — a daily-send ramp so a new mailbox doesn't blast full volume
   on day one ([warmup.py](warmup.py)).
2. **Safe scale across multiple mailboxes** — automatic rotation across a
   pool of mailboxes, each with its own independent warmup clock and daily
   cap ([mailboxes.py](mailboxes.py)); strict sequence ordering and
   suppression checked before every single send ([sender.py](sender.py),
   [db.py](db.py)).
3. **Reply/bounce auto-detection** — IMAP polling that classifies incoming
   mail and reacts correctly per type, including telling a permanent bounce
   apart from a transient one ([reply_watcher.py](reply_watcher.py)).
4. **Deliverability health checks** — SPF/DMARC/DKIM verification and DNS
   blacklist checks before a real campaign, plus a proper `List-Unsubscribe`
   header (Gmail/Yahoo have required this for bulk senders since Feb 2024)
   ([deliverability.py](deliverability.py), [smtp_sender.py](smtp_sender.py)).
5. **Campaign reporting** — sent/reply/bounce rates, per-mailbox breakdown,
   daily volume — the free equivalent of Smartlead's dashboard basics, all
   from data already tracked ([reports.py](reports.py)).

Decoupled from content on purpose: this takes a JSON queue of already-written
messages (`{to_email, subject, body, step}`). It doesn't care whether that
content came from Stage 5's drafting (not built yet — still need your email
template) or a manual batch you wrote today. Plug either in.

## What this can't do that Smartlead can

These are structural, not code gaps — closing them for free isn't a matter
of writing more of this project, the same way Apollo's database or
Firecrawl's CAPTCHA-bypass infra aren't:

- **No managed warmup network.** Smartlead's warmup sends/receives fake
  engagement across its customer network (thousands of other paying
  customers' mailboxes) to build reputation faster. This just paces YOUR
  real sends slowly — genuine but slower reputation building. A free tool
  with one user has no network to draw on.
- **No live inbox-placement testing.** Smartlead (and dedicated tools) can
  check whether your sends actually land in the inbox vs. spam, using seed
  accounts across Gmail/Outlook/Yahoo. That needs owning many real seed
  mailboxes across providers — no free equivalent. `deliverability.py`
  gets you the closest free proxy (SPF/DMARC/blacklist health), but it's a
  proxy for deliverability risk, not a direct placement measurement. Watch
  your reply rate as the other real-world signal.
- **Reply classification is rule-based**, not AI-nuanced. It reliably tells
  bounce / unsubscribe / out-of-office / "this is a real reply" apart, but
  it does NOT tell you if a real reply is interested vs. a polite no — a
  human reads every genuine reply (as designed — never auto-answer a hot
  lead). Layering an LLM call on top of "this is a genuine reply" for
  interested/not-interested nuance is a good next step, not rebuilt here.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# edit .env: SMTP_USER, SMTP_PASSWORD (app password), PHYSICAL_ADDRESS, WARMUP_START_DATE
```

**Gmail/Workspace app password:** enable 2FA, then
https://myaccount.google.com/apppasswords — use that, never your real
login password.

**PHYSICAL_ADDRESS is not optional.** CAN-SPAM requires a real postal
address in every commercial email. `sender.py` refuses to send without it
(`Config.require_send_ready()`).

## Run

```bash
# single mailbox (.env-driven) -- ALWAYS dry-run a new queue file first
python sender.py --queue queue.json --dry-run

# send for real, capped at 5 for a controlled first test
python sender.py --queue queue.json --limit 5

# normal run -- respects the warmup daily cap automatically
python sender.py --queue queue.json

# multiple mailboxes, rotated automatically -- see mailboxes.example.json
cp mailboxes.example.json mailboxes.json  # then fill in real credentials
python sender.py --queue queue.json --mailboxes mailboxes.json --dry-run
python sender.py --queue queue.json --mailboxes mailboxes.json

# check for replies/bounces (run every 15-30 min via cron/Task Scheduler)
python reply_check.py

# pre-flight deliverability check (also runs automatically before a real send)
python -c "from deliverability import run_health_check; import json; print(json.dumps(run_health_check('yourdomain.com'), indent=2))"

# campaign report
python reports.py
```

`sender.py` runs the SPF/DMARC check automatically before any real (non-dry-run)
send — pass `--skip-preflight` to skip it. It only warns, never blocks (a
transient DNS hiccup shouldn't stop a real campaign).

### Multi-mailbox rotation

Pass `--mailboxes mailboxes.json` (array of mailbox configs, each with its
own SMTP credentials and `warmup_start_date`) instead of relying on the
single `.env` mailbox. Every send picks whichever mailbox in the pool has
the **most remaining capacity today** — not simple round-robin — so a
mailbox that started warming up last week and one that started 6 weeks ago
each fill up proportional to their own ramp stage, instead of the newer one
getting blasted at the same rate as the fully-warmed one. `PHYSICAL_ADDRESS`
in `.env` is still required (shared company identity across all mailboxes);
per-mailbox SMTP/IMAP credentials live only in `mailboxes.json`.

### Queue file format

```json
[
  {"to_email": "jane@acme.com", "to_name": "Jane Diaz",
   "subject": "quick question", "body": "...", "step": 1}
]
```

`step` must proceed in order per lead (1, then 2, then 3, then 4) — the
engine enforces this and skips anything out of order rather than silently
sending a follow-up before the first email. Steps 2-4 are automatically
threaded as replies to step 1 (same subject line with `Re:`, proper
`In-Reply-To`/`References` headers) so the sequence reads as one
conversation, not four separate cold emails.

## How sending safety actually works here

- **Suppression checked before every send**, not just at queue-build time —
  `db.is_suppressed()` covers both exact-email and domain-wide suppression.
  A reply that says "stop" suppresses the whole domain (a colleague won't
  get emailed next week by the same campaign); a bounce suppresses only
  that one address (bouncing isn't "they said no").
- **Daily cap comes from the warmup ramp**, computed from `WARMUP_START_DATE`
  — 10/day week 1, 20/day week 2, 30/day week 3, 40/day steady state. Once
  the mailbox hits its cap for the day, the run stops cleanly; remaining
  queue items get picked up on the next run/day, nothing is lost or skipped
  permanently.
- **A failed send never advances the sequence** — if SMTP throws an error
  for one lead, that lead stays at its current step and gets logged as
  failed, but the rest of the batch keeps going.
- **Randomized delay** (default 60-180s, configurable) between sends —
  avoids a suspicious burst pattern.

## Reply handling logic

| Classification | Detected by | Action |
|---|---|---|
| `bounce_hard` | DSN `Status: 5.x.x`, or keywords like "no such user"/"does not exist" | suppress that one email **permanently** |
| `bounce_soft` | DSN `Status: 4.x.x`, or keywords like "mailbox full"/"try again later" | reschedule retry in 2 days — **NOT suppressed.** A transient mail-server hiccup is not "this address doesn't exist"; treating every bounce as permanent was a real bug caught and fixed while building this — see `test_poll_and_process_soft_bounce_does_not_suppress` |
| `unsubscribe` | body/subject contains unsubscribe/stop/remove-me phrasing | suppress email **and whole domain** permanently |
| `ooo` (out of office) | subject/body contains auto-reply/vacation phrasing | reschedule next send (parses a return date if present, else +7 days) — does NOT count as a reply |
| `reply` (anything else) | default | **stop that lead's sequence, flag for human review.** Never auto-suppresses the domain — a reply isn't necessarily "no" |

Bounce severity is checked against the machine-readable DSN status code
first (most reliable when the sending server includes one), falling back
to keyword matching. A genuinely ambiguous bounce defaults to "hard" —
safer to suppress an unclear case than keep hammering a possibly-broken
mailbox, a documented judgment call, not a certainty.

Polling uses `BODY.PEEK` (not a plain fetch), so checking for replies never
marks your real inbox's messages as read out from under you if this is also
an account you read manually. Its own dedup table means a re-run never
double-processes the same message.

## Deliverability health checks

`deliverability.py` — standard DNS queries, no API key, no account:

- **SPF** — is a Sender Policy Framework record published for the domain?
- **DMARC** — is a DMARC record published, and what's the policy
  (`none`/`quarantine`/`reject`)?
- **DKIM** — best-effort check against common selector names (`google`,
  `default`, `selector1`, `selector2`, `k1`, `dkim`, `mail`). An empty
  result does **not** prove DKIM is unconfigured — it just means none of
  the common guesses matched; check your ESP's real selector if you need
  certainty.
- **DNS blacklists** — checks the sending IP against public DNSBL zones
  (Spamhaus ZEN, Barracuda, SORBS) via direct DNS lookup. Fine for
  checking your own IP occasionally before a campaign; not built for
  high-volume querying (Spamhaus in particular has usage policies around
  that).

All four verified live against real DNS while building this — confirmed
correct on gmail.com's actual SPF/DMARC records and basecamp.com's, and
confirmed a known-clean IP (8.8.8.8) reports no blacklist hits.

`sender.py` runs SPF+DMARC automatically before a real send and logs a
warning (never blocks) if either is missing.

## Campaign reporting

`reports.py` — the free equivalent of Smartlead's dashboard, from data
`db.py` already tracks:

```bash
python reports.py
```

```
=== Campaign Summary ===
Total leads:        142
  active          98
  replied         12
  bounced         4
  ...
Total sent:         210
Replied:            12 (5.7%)
Bounced:            4 (1.9%)

=== By Mailbox ===
  sales1@yourdomain.com          sent=  105  failed=1
  sales2@yourdomain.com          sent=  105  failed=0

=== Daily Send Volume (last 14 days) ===
  2026-09-08  30
  ...
```

## Testing

```bash
pip install pytest aiosmtpd
python -m pytest tests/ -v
```

118 tests. Most SMTP/IMAP behavior is mocked for determinism, but
`test_smtp_sender.py` also runs the actual send path against a **real local
SMTP server** (`aiosmtpd`, localhost-only, nothing touches the network) —
genuine protocol-level verification of message construction and threading
headers, not just "was the mock called."

**No real email is ever sent as part of building or testing this** — that
would be an outward-facing action requiring your explicit go-ahead with
real credentials, which this session doesn't have and won't act on
unprompted.

## What's still needed to actually go live

1. **SMTP/IMAP credentials** for the sending account (app password).
2. **Physical mailing address** (legal requirement).
3. **Warmup time** — if this mailbox hasn't been sending real mail before,
   start the ramp NOW even before content is ready; 3-4 weeks of runway
   before your first real cold-send batch is the standard guidance.
4. **Actual email content** — Stage 5 (drafting against your template)
   isn't built yet; still need your email template + what you sell/ICP/proof
   points to build that. Until then, this can send a manually-written batch
   if you want to start warming up with a small number of real, honest
   test sends to people who've agreed to hear from you, or to yourself.

## Compliance reminder

CAN-SPAM (US): physical address + honest subject/from + working opt-out,
honored within 10 days — all handled by the footer + suppression logic here,
but you're the one who has to actually mean it (don't re-add someone who
unsubscribed to a different list). CASL (Canada) and GDPR/PECR (UK/EU) have
stricter consent requirements this engine does not adjudicate — that's a
list-hygiene decision made upstream (Stage 1/9 of the original design), not
something `sender.py` can determine from an email address alone.
