"""Deterministic per-participant calendar generator.

Replaces the abandoned Solar-Pro-3 calendar synthesis path (which couldn't
control density or guarantee answer-slot survival). Pure-Python, seeded
by ``scenario_id`` so the output is reproducible from the input alone.

Function contract (per the C4a-cal brief):
    generate_calendar(
        scenario_id: str,
        participants: list[str],
        reference_date: str,            # 'YYYY-MM-DD'
        density: str,                   # '빡빡' | '보통' | '여유'
        guaranteed_slots: list[dict] | None = None,
        is_trap: bool = False,
    ) -> dict[str, list[dict]]

Range: reference_date − 7 days .. reference_date + 7 days, half-open
       (e.g., reference 2026-05-11 → [2026-05-04T00:00, 2026-05-18T00:00)).

Density (windows per participant per day, window length range in hours):
    빡빡: 0–1 windows / day, 0.5–2.0 h each
    보통: 1–2 windows / day, 1.0–3.0 h each
    여유: 1–3 windows / day, 2.0–6.0 h each

Realistic constraints (hardcoded — no LLM prompt involved):
    * 00:00–06:00 is sleep; windows never overlap this block.
    * On weekdays (Mon–Fri), the 09:00–18:00 block is open with 30% probability
      (assumes work/school), independent per participant per day.
    * Meal slots (12:00–13:00, 18:00–19:00) are open with 50% probability
      on weekdays. On weekends every block is fully open.

Guaranteed-slot mechanics:
    * Non-trap: every slot in ``guaranteed_slots`` is added to every
      participant's free-time list (the post-merge ensures it survives
      intersection).
    * Trap: one ``guaranteed_slot`` is randomly chosen and removed from
      one randomly chosen participant — at least one participant becomes
      unavailable for that slot, so intersection drops it.

Validation (internal, retry up to 3× with different seeds):
    * Every window: start < end, both ISO 8601 local 'YYYY-MM-DDTHH:MM'.
    * Per-participant windows sorted by start, non-overlapping (we merge
      overlaps post-injection).
    * No window crosses into 00:00–06:00.
    * Non-trap: intersection (30-min grid) contains every guaranteed slot.
    * Trap: intersection contains at most ``max(0, len(guaranteed)-1)``
      guaranteed slots — engineered to be smaller.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable


SLEEP_START_H = 0   # 00:00 inclusive
SLEEP_END_H = 6     # 06:00 exclusive

WORKDAY_START_H = 9
WORKDAY_END_H = 18

MEAL_BLOCKS = [(12, 13), (18, 19)]   # 12:00-13:00, 18:00-19:00 (half-open)


DENSITY_PARAMS = {
    "빡빡": {"windows_per_day": (0, 1), "length_hours": (0.5, 2.0)},
    "보통": {"windows_per_day": (1, 2), "length_hours": (1.0, 3.0)},
    "여유": {"windows_per_day": (1, 3), "length_hours": (2.0, 6.0)},
}


# ---- public API ------------------------------------------------------------

def generate_calendar(
    scenario_id: str,
    participants: list[str],
    reference_date: str,
    density: str,
    guaranteed_slots: list[dict] | None = None,
    is_trap: bool = False,
) -> dict[str, list[dict]]:
    if density not in DENSITY_PARAMS:
        raise ValueError(f"unknown density: {density!r}")
    if not participants:
        raise ValueError("participants must be non-empty")
    guaranteed_slots = list(guaranteed_slots or [])

    base_seed = int(hashlib.sha256(scenario_id.encode()).hexdigest()[:8], 16)
    last_err: str | None = None
    for retry in range(3):
        rng = random.Random(base_seed + retry)
        cal = _build_calendars(
            rng, participants, reference_date, density,
            guaranteed_slots, is_trap,
        )
        ok, err = _validate(cal, guaranteed_slots, is_trap)
        if ok:
            return {p: [w.to_dict() for w in ws] for p, ws in cal.items()}
        last_err = err
    raise RuntimeError(
        f"generate_calendar({scenario_id}): exhausted 3 seeds; last err={last_err}"
    )


# ---- internals -------------------------------------------------------------

@dataclass(frozen=True)
class _Window:
    start: datetime
    end: datetime

    def to_dict(self) -> dict:
        return {
            "start": self.start.strftime("%Y-%m-%dT%H:%M"),
            "end": self.end.strftime("%Y-%m-%dT%H:%M"),
        }


def _parse(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _range_days(reference_date: str) -> list[datetime]:
    base = _parse(reference_date + "T00:00")
    return [base + timedelta(days=offset) for offset in range(-7, 7)]   # 14 days


def _is_weekend(d: datetime) -> bool:
    return d.weekday() >= 5    # Sat=5, Sun=6


def _candidate_starts(rng: random.Random, day: datetime, length_h: float, n_per_day: int,
                      is_weekend: bool) -> list[datetime]:
    """Sample window start times within the open hours of `day`."""
    # Open hours: [06:00, 24:00); apply weekday work/meal restrictions.
    blocked: list[tuple[float, float]] = []
    if not is_weekend:
        # Workday block: closed 30% of the time → independent gate per day.
        if rng.random() < 0.7:
            blocked.append((WORKDAY_START_H, WORKDAY_END_H))
        # Meal blocks: closed 50% of the time.
        for s, e in MEAL_BLOCKS:
            if rng.random() < 0.5:
                blocked.append((float(s), float(e)))
    # Merge blocked
    blocked.sort()
    merged: list[tuple[float, float]] = []
    for s, e in blocked:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    # Open intervals = [SLEEP_END_H, 24] minus merged.
    open_intervals: list[tuple[float, float]] = []
    cursor = float(SLEEP_END_H)
    for s, e in merged:
        if s > cursor:
            open_intervals.append((cursor, s))
        cursor = max(cursor, e)
    if cursor < 24.0:
        open_intervals.append((cursor, 24.0))
    # For each desired window, find an interval that fits its length_h, sample start.
    starts: list[datetime] = []
    attempts = 0
    while len(starts) < n_per_day and attempts < 20:
        attempts += 1
        feasible = [iv for iv in open_intervals if iv[1] - iv[0] >= length_h]
        if not feasible:
            break
        iv = rng.choice(feasible)
        start_h = rng.uniform(iv[0], iv[1] - length_h)
        # Round to nearest 30 min for tidiness.
        start_h_rounded = round(start_h * 2) / 2
        if start_h_rounded + length_h > iv[1]:
            start_h_rounded = iv[1] - length_h
        start_dt = day + timedelta(hours=start_h_rounded)
        starts.append(start_dt)
        # Subtract the slot from open intervals so the next pick doesn't overlap.
        new_open = []
        for jv in open_intervals:
            if jv == iv:
                if start_h_rounded > jv[0]:
                    new_open.append((jv[0], start_h_rounded))
                if start_h_rounded + length_h < jv[1]:
                    new_open.append((start_h_rounded + length_h, jv[1]))
            else:
                new_open.append(jv)
        open_intervals = new_open
    return starts


def _gen_one_participant(rng: random.Random, days: list[datetime], density: str) -> list[_Window]:
    p = DENSITY_PARAMS[density]
    win_lo, win_hi = p["windows_per_day"]
    len_lo, len_hi = p["length_hours"]
    out: list[_Window] = []
    for day in days:
        # On weekends, lean toward more windows; the rng range is [win_lo, win_hi].
        n = rng.randint(win_lo, win_hi)
        if n == 0:
            continue
        length = round(rng.uniform(len_lo, len_hi) * 2) / 2   # 0.5-h grid
        is_weekend = _is_weekend(day)
        starts = _candidate_starts(rng, day, length, n, is_weekend)
        for s in starts:
            out.append(_Window(s, s + timedelta(hours=length)))
    return _merge_sorted(out)


def _merge_sorted(windows: Iterable[_Window]) -> list[_Window]:
    sorted_ws = sorted(windows, key=lambda w: w.start)
    out: list[_Window] = []
    for w in sorted_ws:
        if out and w.start <= out[-1].end:
            out[-1] = _Window(out[-1].start, max(out[-1].end, w.end))
        else:
            out.append(w)
    return out


def _inject_guaranteed(
    rng: random.Random,
    cal: dict[str, list[_Window]],
    guaranteed: list[dict],
    is_trap: bool,
) -> dict[str, list[_Window]]:
    if not guaranteed:
        return cal
    slots = [_Window(_parse(g["start"]), _parse(g["end"])) for g in guaranteed]
    participants = list(cal.keys())

    if is_trap:
        # Pick one slot, pick one participant who will NOT have it. Everyone
        # else gets it. The blocked participant also has any overlapping
        # existing window pre-removed so the slot truly isn't covered.
        slot_to_block = rng.choice(slots)
        blocker = rng.choice(participants)
        for p in participants:
            ws = cal[p]
            if p == blocker:
                # Remove overlap with slot_to_block.
                trimmed: list[_Window] = []
                for w in ws:
                    pieces = _subtract(w, slot_to_block)
                    trimmed.extend(pieces)
                cal[p] = _merge_sorted(trimmed)
            else:
                cal[p] = _merge_sorted(ws + [slot_to_block])
        # The non-blocked slots: inject into everyone normally (intersection
        # would otherwise be 0; we want 0-1 surviving slots, not 0).
        for slot in slots:
            if slot == slot_to_block:
                continue
            for p in participants:
                cal[p] = _merge_sorted(cal[p] + [slot])
    else:
        for slot in slots:
            for p in participants:
                cal[p] = _merge_sorted(cal[p] + [slot])
    return cal


def _subtract(window: _Window, hole: _Window) -> list[_Window]:
    """Return ``window`` minus ``hole`` as ≤ 2 sub-windows."""
    if hole.end <= window.start or hole.start >= window.end:
        return [window]
    pieces: list[_Window] = []
    if hole.start > window.start:
        pieces.append(_Window(window.start, min(hole.start, window.end)))
    if hole.end < window.end:
        pieces.append(_Window(max(hole.end, window.start), window.end))
    return [p for p in pieces if p.end > p.start]


def _build_calendars(
    rng: random.Random,
    participants: list[str],
    reference_date: str,
    density: str,
    guaranteed: list[dict],
    is_trap: bool,
) -> dict[str, list[_Window]]:
    days = _range_days(reference_date)
    cal: dict[str, list[_Window]] = {}
    for p in participants:
        cal[p] = _gen_one_participant(rng, days, density)
    cal = _inject_guaranteed(rng, cal, guaranteed, is_trap)
    return cal


# ---- validation ------------------------------------------------------------

def _slot_set(windows: list[_Window]) -> set[str]:
    out: set[str] = set()
    for w in windows:
        t = w.start
        while t < w.end:
            out.add(t.strftime("%Y-%m-%dT%H:%M"))
            t += timedelta(minutes=30)
    return out


def _intersection_slots(cal: dict[str, list[_Window]]) -> set[str]:
    sets = [_slot_set(ws) for ws in cal.values()]
    return set.intersection(*sets) if sets else set()


def _guaranteed_present(intersection: set[str], guaranteed: list[dict]) -> int:
    """Count how many guaranteed slots are fully inside the intersection."""
    n = 0
    for g in guaranteed:
        gs = _slot_set([_Window(_parse(g["start"]), _parse(g["end"]))])
        if gs.issubset(intersection):
            n += 1
    return n


def _validate(
    cal: dict[str, list[_Window]],
    guaranteed: list[dict],
    is_trap: bool,
) -> tuple[bool, str | None]:
    # Per-window invariants.
    for p, ws in cal.items():
        prev_end: datetime | None = None
        for w in ws:
            if not (w.start < w.end):
                return False, f"{p}: start>=end {w}"
            if w.start.hour < SLEEP_END_H or (w.start.hour == SLEEP_END_H and w.start.minute < 0):
                return False, f"{p}: window starts before 06:00 ({w.start})"
            # Window must not pass through 06:00 from below — but since starts
            # are ≥ 06:00 already, end ≤ next-day boundary is fine. Ends may
            # legitimately reach midnight.
            if prev_end is not None and w.start < prev_end:
                return False, f"{p}: overlapping windows {prev_end} > {w.start}"
            prev_end = w.end
    inter = _intersection_slots(cal)
    n_guaranteed = _guaranteed_present(inter, guaranteed)
    if guaranteed:
        if is_trap:
            # Want strictly fewer guaranteed slots than provided.
            target = max(0, len(guaranteed) - 1)
            if n_guaranteed > target:
                return False, f"trap: too many guaranteed slots survived ({n_guaranteed}/{len(guaranteed)})"
        else:
            if n_guaranteed != len(guaranteed):
                return False, f"non-trap: only {n_guaranteed}/{len(guaranteed)} guaranteed slots survived"
    return True, None


# ---- public helpers --------------------------------------------------------

def intersection_30min(cal: dict[str, list[dict]]) -> set[str]:
    """Compute the 30-min slot intersection across all participants.

    Used by labeling/measurement to count how many slots survive after the
    AND across every participant's free time. Returns a set of ISO 8601
    'YYYY-MM-DDTHH:MM' strings.
    """
    sets = []
    for windows in cal.values():
        s: set[str] = set()
        for w in windows:
            t = _parse(w["start"])
            e = _parse(w["end"])
            while t < e:
                s.add(t.strftime("%Y-%m-%dT%H:%M"))
                t += timedelta(minutes=30)
        sets.append(s)
    return set.intersection(*sets) if sets else set()
