"""Unit tests for scripts/data/generate_calendar.py.

Five cases per the C4a-cal-1 brief:
    1. test_seeded_deterministic   — same scenario_id → byte-identical output.
    2. test_density_빡빡            — windows-per-day count + length matrix.
    3. test_density_여유            — windows-per-day count + length matrix.
    4. test_realistic_constraints   — 0 windows touching 00:00–06:00.
    5. test_answer_slots_guaranteed — non-trap: every guaranteed slot
                                      survives intersection.

No external I/O, no Solar calls.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta

from data.generate_calendar import (
    DENSITY_PARAMS,
    generate_calendar,
    intersection_30min,
)


REFERENCE = "2026-05-11"
PEOPLE_3 = ["민지", "준호", "지수"]


def _report(name: str, failures: list[str]) -> bool:
    if failures:
        print(f"[FAIL] {name}")
        for f in failures:
            print(f"   - {f}")
        return False
    print(f"[OK]   {name}")
    return True


def _parse(s: str) -> datetime:
    return datetime.fromisoformat(s)


# ---- cases -----------------------------------------------------------------

def case_seeded_deterministic() -> bool:
    a = generate_calendar("p07", PEOPLE_3, REFERENCE, "보통")
    b = generate_calendar("p07", PEOPLE_3, REFERENCE, "보통")
    failures = []
    if json.dumps(a, ensure_ascii=False, sort_keys=True) != json.dumps(b, ensure_ascii=False, sort_keys=True):
        failures.append("byte diff between two runs with same scenario_id")
    # A different scenario_id should produce different output (not strictly
    # required, but worth a sanity check).
    c = generate_calendar("p99-different", PEOPLE_3, REFERENCE, "보통")
    if json.dumps(a, sort_keys=True) == json.dumps(c, sort_keys=True):
        failures.append("different scenario_ids produced identical calendars")
    return _report("seeded_deterministic", failures)


def _check_density(cal, density: str, expect_min_days: int) -> list[str]:
    """Post-merge density check.

    The generator merges adjacent windows, so a "1~3 windows × 2~6 h each"
    spec for 여유 can collapse into a single 10-hour block. We test the
    post-merge upper bound (windows_per_day_hi × length_hi) instead of the
    per-window length cap, plus daily coverage and per-window minimum.
    """
    win_lo, win_hi = DENSITY_PARAMS[density]["windows_per_day"]
    len_lo, len_hi = DENSITY_PARAMS[density]["length_hours"]
    max_merged_h = win_hi * len_hi
    failures: list[str] = []
    for p, windows in cal.items():
        by_day: dict[str, int] = {}
        for w in windows:
            day = w["start"][:10]
            by_day[day] = by_day.get(day, 0) + 1
            length = (_parse(w["end"]) - _parse(w["start"])).total_seconds() / 3600
            if length < len_lo - 0.01:
                failures.append(f"{p} day {day}: length {length}h below min {len_lo}")
            if length > max_merged_h + 0.1:
                failures.append(
                    f"{p} day {day}: length {length}h exceeds post-merge cap {max_merged_h}h"
                )
        if len(by_day) < expect_min_days:
            failures.append(f"{p}: only {len(by_day)} days have windows (expected ≥{expect_min_days})")
        for day, n in by_day.items():
            if n > win_hi:
                failures.append(f"{p} day {day}: {n} disjoint windows exceeds limit {win_hi}")
    return failures


def case_density_빡빡() -> bool:
    cal = generate_calendar("p13", PEOPLE_3, REFERENCE, "빡빡")
    # 빡빡 allows 0 windows/day, so daily coverage may be sparse.
    return _report("density_빡빡", _check_density(cal, "빡빡", expect_min_days=0))


def case_density_여유() -> bool:
    cal = generate_calendar("p01", PEOPLE_3, REFERENCE, "여유")
    # 여유 expects ≥1 window most days.
    return _report("density_여유", _check_density(cal, "여유", expect_min_days=7))


def case_realistic_constraints() -> bool:
    failures = []
    for sid in ("p01", "p07", "p13", "p20", "p25"):
        density = {"p01": "여유", "p07": "빡빡", "p13": "빡빡",
                   "p20": "빡빡", "p25": "여유"}[sid]
        cal = generate_calendar(sid, PEOPLE_3, REFERENCE, density)
        for p, windows in cal.items():
            for w in windows:
                s = _parse(w["start"])
                e = _parse(w["end"])
                if s.hour < 6:
                    failures.append(f"{sid} {p}: window starts before 06:00 → {w}")
                # End is exclusive; allow exactly 00:00 (next day) but disallow
                # any window whose body actually overlaps the sleep block.
                # Body overlaps 00:00–06:00 iff start.hour < 6 (already caught)
                # or end day != start day and end.hour > 0.
                if e.date() != s.date() and not (e.hour == 0 and e.minute == 0):
                    failures.append(f"{sid} {p}: window crosses into sleep block → {w}")
    return _report("realistic_constraints", failures)


def case_answer_slots_guaranteed() -> bool:
    failures = []
    # Two guaranteed slots; both must survive the intersection.
    guaranteed = [
        {"start": "2026-05-15T19:00", "end": "2026-05-15T21:00"},   # Fri evening
        {"start": "2026-05-16T14:00", "end": "2026-05-16T16:00"},   # Sat afternoon
    ]
    cal = generate_calendar(
        "p_test_guaranteed", PEOPLE_3, REFERENCE, "보통",
        guaranteed_slots=guaranteed, is_trap=False,
    )
    inter = intersection_30min(cal)
    # Each guaranteed slot is 2 hours = 4 thirty-min slots.
    for g in guaranteed:
        t = _parse(g["start"])
        end = _parse(g["end"])
        while t < end:
            label = t.strftime("%Y-%m-%dT%H:%M")
            if label not in inter:
                failures.append(f"guaranteed slot missing from intersection: {label}")
            t += timedelta(minutes=30)
    # Trap negative check: with the same guaranteed slots and is_trap=True,
    # the surviving intersection must contain FEWER than all guaranteed.
    trap_cal = generate_calendar(
        "p_test_trap", PEOPLE_3, REFERENCE, "빡빡",
        guaranteed_slots=guaranteed, is_trap=True,
    )
    trap_inter = intersection_30min(trap_cal)
    n_guaranteed_in_trap = sum(
        1 for g in guaranteed
        if all(
            (_parse(g["start"]) + timedelta(minutes=30 * i)).strftime("%Y-%m-%dT%H:%M") in trap_inter
            for i in range(int((_parse(g["end"]) - _parse(g["start"])).total_seconds() // 1800))
        )
    )
    if n_guaranteed_in_trap >= len(guaranteed):
        failures.append(
            f"trap case: {n_guaranteed_in_trap}/{len(guaranteed)} survived; "
            "expected strictly fewer"
        )
    return _report("answer_slots_guaranteed", failures)


def main() -> int:
    cases = [
        case_seeded_deterministic,
        case_density_빡빡,
        case_density_여유,
        case_realistic_constraints,
        case_answer_slots_guaranteed,
    ]
    results = [c() for c in cases]
    passed = sum(results)
    print(f"\nSUMMARY: {passed}/{len(results)} generate_calendar cases passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
