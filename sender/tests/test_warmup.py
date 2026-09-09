import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from warmup import compute_daily_cap, days_since_start, warmup_stage_label, DEFAULT_RAMP


def test_days_since_start_zero_on_start_day():
    assert days_since_start(date(2026, 1, 1), date(2026, 1, 1)) == 0


def test_days_since_start_never_negative_for_future_start():
    # a start date in the future (misconfigured) shouldn't produce negative offsets
    assert days_since_start(date(2026, 6, 1), date(2026, 1, 1)) == 0


def test_compute_daily_cap_week_1():
    assert compute_daily_cap(date(2026, 1, 1), date(2026, 1, 3)) == 10


def test_compute_daily_cap_week_2():
    assert compute_daily_cap(date(2026, 1, 1), date(2026, 1, 10)) == 20


def test_compute_daily_cap_week_3():
    assert compute_daily_cap(date(2026, 1, 1), date(2026, 1, 17)) == 30


def test_compute_daily_cap_steady_state():
    assert compute_daily_cap(date(2026, 1, 1), date(2026, 3, 1)) == 40


def test_compute_daily_cap_boundary_day_7_moves_to_week_2():
    assert compute_daily_cap(date(2026, 1, 1), date(2026, 1, 8)) == 20  # day offset 7 exactly


def test_warmup_stage_label_week_1():
    assert "week 1" in warmup_stage_label(date(2026, 1, 1), date(2026, 1, 3))


def test_warmup_stage_label_steady_state():
    assert warmup_stage_label(date(2026, 1, 1), date(2026, 3, 1)) == "steady state"


def test_ramp_table_covers_all_offsets_with_no_gaps():
    # sanity check on the table itself -- every day from 0 to 100 must match something
    for day in range(0, 100):
        matched = any(start <= day and (end is None or day < end) for start, end, _ in DEFAULT_RAMP)
        assert matched, f"day {day} not covered by any ramp entry"
