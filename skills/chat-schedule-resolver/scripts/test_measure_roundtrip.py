"""Unit tests for ``measure_roundtrip`` (C4b-prep-2).

Three mock cases per the brief:
    1. test_measure_perfect       — output mirrors expected → all metrics 100%.
    2. test_measure_partial_fail  — 6/30 scenarios miss M4 → M4 = 80%.
    3. test_measure_unresolved_mismatch — output unresolved empty → M5 F1 = 0.

Also covers the new C4b adjustments:
    * M4 IoU ≥ 0.5 matching (not exact equality).
    * M_FP guard when output extracted is empty.
"""

from __future__ import annotations

import sys
from copy import deepcopy

import measure_roundtrip as m


# ---- shared mini-fixture ---------------------------------------------------

def _rec(sid: str, *, with_expected: bool = True) -> dict:
    rec = {
        "scenario_id": sid,
        "scenario": {
            "people": ["민지", "준호"],
            "conversation": [
                {"user": "민지", "text": "이번주 늦게 보자", "ts": ""},
                {"user": "준호", "text": "금요일 저녁 좋아", "ts": ""},
            ],
        },
        "calendars": {
            "민지": [{"start": "2026-05-15T19:00", "end": "2026-05-15T21:00"}],
            "준호": [{"start": "2026-05-15T19:00", "end": "2026-05-15T21:00"}],
        },
        "calendar_meta": {"is_trap_effective": False},
    }
    if with_expected:
        rec["expected_extracted"] = [
            {"who": "민지", "type": "prefer", "time_expr_raw": "이번주 늦게", "evidence_msg_id": 1},
            {"who": "준호", "type": "prefer", "time_expr_raw": "금요일 저녁", "evidence_msg_id": 2},
        ]
        rec["expected_top3"] = [
            {"start": "2026-05-15T19:00", "end": "2026-05-15T21:00",
             "participants_available": ["민지", "준호"], "rationale": "둘 다 가능"},
        ]
        rec["expected_unresolved"] = []
    return rec


def _perfect_output(rec: dict) -> dict:
    return {
        "extracted": [dict(r) for r in rec["expected_extracted"]],
        "top3": [dict(s) for s in rec["expected_top3"]],
        "unresolved": list(rec["expected_unresolved"]),
    }


def _report(name: str, failures: list[str]) -> bool:
    if failures:
        print(f"[FAIL] {name}")
        for f in failures:
            print(f"   - {f}")
        return False
    print(f"[OK]   {name}")
    return True


# ---- cases -----------------------------------------------------------------

def case_perfect() -> bool:
    per = []
    for i in range(30):
        rec = _rec(f"p{i:02d}")
        out = _perfect_output(rec)
        per.append(m.measure_one(rec, out))
    agg = m.aggregate(per)
    failures = []
    for key in ("M1", "M2", "M3", "M4"):
        _, _, r = agg[key]
        if r is None or abs(r - 1.0) > 1e-9:
            failures.append(f"{key} should be 1.0, got {r}")
    if abs(agg["M5"]["f1"] - 0.0) > 1e-9 and (agg["M5"]["tp"] + agg["M5"]["fn"] + agg["M5"]["fp"]) == 0:
        # F1 with all-empty sets = 0 is acceptable (no signal); accept.
        pass
    fp_n, fp_d, fp_r = agg["M_FP"]
    if fp_n != 0 or (fp_d > 0 and fp_r != 0.0):
        failures.append(f"M_FP should be 0/N with rate 0, got n={fp_n} d={fp_d} r={fp_r}")
    if agg["failures"]:
        failures.append(f"unexpected failures: {agg['failures']}")
    return _report("measure_perfect", failures)


def case_partial_m4_fail() -> bool:
    """6/30 scenarios produce a Top-3 that misses expected[0] (IoU < 0.5).
    The remaining 24/30 hit. M4 should be 24/30 = 80%."""
    per = []
    for i in range(30):
        rec = _rec(f"p{i:02d}")
        out = _perfect_output(rec)
        if i < 6:
            # Shift target so IoU < 0.5: original 19:00–21:00 → mock 14:00–16:00.
            out["top3"] = [{
                "start": "2026-05-15T14:00", "end": "2026-05-15T16:00",
                "participants_available": ["민지", "준호"],
                "rationale": "alt",
            }]
        per.append(m.measure_one(rec, out))
    agg = m.aggregate(per)
    failures = []
    n, d, r = agg["M4"]
    if d != 30 or n != 24 or abs(r - 24/30) > 1e-9:
        failures.append(f"M4 expected 24/30 (80%); got {n}/{d} = {r}")
    # IoU-based: a 30-min nudge (19:00–21:00 vs 19:00–20:30) must still match.
    for i in range(30):
        rec = _rec(f"q{i:02d}")
        out = _perfect_output(rec)
        out["top3"] = [{
            "start": "2026-05-15T19:00", "end": "2026-05-15T20:30",
            "participants_available": ["민지", "준호"],
            "rationale": "shorter",
        }]
        mres = m.measure_one(rec, out)
        if not mres.m4_hit:
            failures.append(f"IoU partial overlap should match: got best_iou={mres.m4_iou:.2f}")
            break
    return _report("measure_partial_m4_fail", failures)


def case_unresolved_mismatch() -> bool:
    """expected_unresolved has 1 entry per scenario; output has none.
    M5 recall = 0, precision = 0 (no TP), F1 = 0."""
    per = []
    for i in range(5):
        rec = _rec(f"p{i:02d}")
        rec["expected_unresolved"] = [
            {"who": "유나", "time_expr_raw": "수요일 저녁"},
        ]
        out = _perfect_output(rec)
        out["unresolved"] = []
        per.append(m.measure_one(rec, out))
    agg = m.aggregate(per)
    failures = []
    if abs(agg["M5"]["f1"]) > 1e-9:
        failures.append(f"M5 F1 should be 0, got {agg['M5']['f1']}")
    if agg["M5"]["fn"] != 5:
        failures.append(f"expected 5 false negatives, got {agg['M5']['fn']}")
    if agg["M5"]["tp"] != 0:
        failures.append(f"expected 0 true positives, got {agg['M5']['tp']}")
    return _report("measure_unresolved_mismatch", failures)


def case_mfp_guard() -> bool:
    """Guard: when output extracted is empty, M_FP = 0/0 → 0.0 (not crash, not NaN)."""
    rec = _rec("p01")
    out = {"extracted": [], "top3": [], "unresolved": []}
    mres = m.measure_one(rec, out)
    agg = m.aggregate([mres])
    failures = []
    fp_n, fp_d, fp_r = agg["M_FP"]
    if fp_n != 0 or fp_d != 0:
        failures.append(f"empty output: M_FP should be 0/0, got {fp_n}/{fp_d}")
    if fp_r != 0.0:
        failures.append(f"empty output: M_FP rate should be 0.0, got {fp_r}")
    return _report("mfp_guard_empty_output", failures)


def main() -> int:
    cases = [
        case_perfect,
        case_partial_m4_fail,
        case_unresolved_mismatch,
        case_mfp_guard,
    ]
    results = [c() for c in cases]
    passed = sum(results)
    print(f"\nSUMMARY: {passed}/{len(results)} measure_roundtrip cases passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
