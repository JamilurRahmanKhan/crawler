"""
Warmup ramp -- the #1 thing that decides whether a mailbox survives cold
sending or gets flagged as spam. A brand-new account blasting 100/day on
day one gets flagged fast; a slow ramp builds sender reputation first.

Pure functions, no I/O -- easy to test, easy to reason about.
"""
from datetime import date

# (day_offset_start_inclusive, day_offset_end_exclusive_or_None, daily_cap)
# Matches the automation design's guidance: start conservative, ramp over
# ~3-4 weeks, steady-state ceiling well under Gmail/Workspace's own limits.
DEFAULT_RAMP = [
    (0, 7, 10),
    (7, 14, 20),
    (14, 21, 30),
    (21, None, 40),
]


def days_since_start(start_date: date, today: date = None) -> int:
    today = today or date.today()
    return max(0, (today - start_date).days)


def compute_daily_cap(start_date: date, today: date = None, ramp: list = None) -> int:
    """How many sends this mailbox is allowed today, per the ramp schedule."""
    ramp = ramp or DEFAULT_RAMP
    offset = days_since_start(start_date, today)
    for start, end, cap in ramp:
        if offset >= start and (end is None or offset < end):
            return cap
    return ramp[-1][2]  # fall through to steady-state cap if ramp table has a gap


def warmup_stage_label(start_date: date, today: date = None) -> str:
    offset = days_since_start(start_date, today)
    if offset < 7:
        return "week 1 (ramping)"
    if offset < 14:
        return "week 2 (ramping)"
    if offset < 21:
        return "week 3 (ramping)"
    return "steady state"
