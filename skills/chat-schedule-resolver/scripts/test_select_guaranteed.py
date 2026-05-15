"""Unit tests for data.select_guaranteed.select_guaranteed_slots.

Three cases per the C4a-cal-2.5 brief:
    1. test_exclude_weekday_filtered  — "월요일 안돼" drops 5/11 (Mon).
    2. test_exclude_weekend_filtered  — "주말 안돼" drops 5/16+5/17.
    3. test_pool_underflow_handled    — restrictive convo drops pool to 0,
                                        auto_trap_flip flips to True and
                                        actual_count goes to 0.

No external I/O. Pure-Python.
"""

from __future__ import annotations

import sys
from datetime import datetime

from data.select_guaranteed import (
    DEFAULT_CANDIDATE_POOL,
    select_guaranteed_slots,
)


def _report(name: str, failures: list[str]) -> bool:
    if failures:
        print(f"[FAIL] {name}")
        for f in failures:
            print(f"   - {f}")
        return False
    print(f"[OK]   {name}")
    return True


def _convo(*lines: str) -> list[dict]:
    return [{"user": "u", "text": t, "ts": ""} for t in lines]


def _is_monday(slot: dict) -> bool:
    return datetime.fromisoformat(slot["start"]).weekday() == 0


def _is_weekend(slot: dict) -> bool:
    return datetime.fromisoformat(slot["start"]).weekday() >= 5


# ---- cases -----------------------------------------------------------------

def case_exclude_weekday_filtered() -> bool:
    convo = _convo("이번 주 모이자!", "월요일은 안돼", "그럼 다른 날 보자")
    out = select_guaranteed_slots(convo, DEFAULT_CANDIDATE_POOL, seed=42)
    failures = []
    if "월" not in out["blocked_weekdays"]:
        failures.append(f"월 not in blocked_weekdays: {out['blocked_weekdays']}")
    if out["block_weekend"]:
        failures.append("block_weekend should be False")
    if any(_is_monday(s) for s in out["selected"]):
        failures.append(f"Monday slot in selected despite exclude: {out['selected']}")
    if not any(_is_monday(s) for s in out["filtered_out"]):
        failures.append("Monday slots should appear in filtered_out")
    if out["auto_trap_flip"]:
        failures.append(f"unexpected trap flip: pool_size_after={out['pool_size_after']}")
    return _report("exclude_weekday_filtered", failures)


def case_exclude_weekend_filtered() -> bool:
    convo = _convo("주말은 안돼", "평일에 모이자")
    out = select_guaranteed_slots(convo, DEFAULT_CANDIDATE_POOL, seed=7)
    failures = []
    if not out["block_weekend"]:
        failures.append("block_weekend should be True")
    if any(_is_weekend(s) for s in out["selected"]):
        failures.append(f"weekend slot in selected: {out['selected']}")
    if not any(_is_weekend(s) for s in out["filtered_out"]):
        failures.append("weekend slots should appear in filtered_out")
    if "토" not in out["blocked_weekdays"] or "일" not in out["blocked_weekdays"]:
        failures.append(f"weekend weekdays missing: {out['blocked_weekdays']}")
    # Also test the "평일만" variant.
    out2 = select_guaranteed_slots(_convo("난 평일만 가능해"), DEFAULT_CANDIDATE_POOL, seed=7)
    if not out2["block_weekend"]:
        failures.append("'평일만' did not trigger weekend block")
    return _report("exclude_weekend_filtered", failures)


def case_pool_underflow_handled() -> bool:
    # Block every weekday + weekend → empty pool.
    convo = _convo(
        "월요일 안돼", "화요일도 빼고", "수요일은 못 가", "목요일 안 되겠다",
        "금요일도 불가", "주말 안돼",
    )
    out = select_guaranteed_slots(convo, DEFAULT_CANDIDATE_POOL, seed=1)
    failures = []
    if out["pool_size_after"] != 0:
        failures.append(f"expected pool_size_after=0, got {out['pool_size_after']}")
    if out["actual_count"] != 0:
        failures.append(f"actual_count should be 0, got {out['actual_count']}")
    if not out["auto_trap_flip"]:
        failures.append("auto_trap_flip should be True when pool empties")
    if out["selected"]:
        failures.append(f"selected should be empty: {out['selected']}")

    # Mid-restriction case: only Mon+Tue blocked → pool size 7, normal selection.
    out2 = select_guaranteed_slots(
        _convo("월요일이랑 화요일은 안돼"), DEFAULT_CANDIDATE_POOL, seed=99,
    )
    if out2["pool_size_after"] != 7:
        failures.append(
            f"mid-restriction pool_size_after expected 7, got {out2['pool_size_after']}"
        )
    if out2["auto_trap_flip"]:
        failures.append("mid-restriction should not auto-trap")
    if not (1 <= out2["actual_count"] <= 3):
        failures.append(f"mid-restriction actual_count out of range: {out2['actual_count']}")
    return _report("pool_underflow_handled", failures)


def case_false_positive_guard() -> bool:
    """'토요일 어려운데 갈게' should NOT exclude Saturday (reversal trap)."""
    # 어려 is not in our exclude tails, so Saturday should remain in the pool.
    convo = _convo("토요일 어려운데 갈게", "오케이 토요일 보자")
    out = select_guaranteed_slots(convo, DEFAULT_CANDIDATE_POOL, seed=3)
    failures = []
    if "토" in out["blocked_weekdays"]:
        failures.append(f"'토요일 어려운데 갈게' wrongly blocked 토: {out}")
    if not any(datetime.fromisoformat(s["start"]).weekday() == 5
               for s in DEFAULT_CANDIDATE_POOL if s not in out["filtered_out"]):
        failures.append("Saturday should remain in available pool")
    return _report("false_positive_guard", failures)


def case_prefer_match_boosts_slot() -> bool:
    """Conversation with '금요일 저녁' multiple times → Friday-evening slot wins #1."""
    convo = _convo(
        "이번주 다같이 보자",
        "금요일 저녁 어때? 종강 회식하자",
        "오 금요일 저녁 좋아",
        "5월 15일 금요일 저녁 7시쯤 모이자",
    )
    out = select_guaranteed_slots(convo, DEFAULT_CANDIDATE_POOL, seed=42)
    failures = []
    if not out["selected"]:
        failures.append("selected is empty")
        return _report("prefer_match_boosts_slot", failures)
    top = out["selected"][0]
    top_wd = datetime.fromisoformat(top["start"]).weekday()
    if top_wd != 4:   # 4 = Friday
        failures.append(
            f"top slot weekday should be 금 (4); got {top_wd} ({top['start']})"
        )
    if out["top_slot_score"] <= 0:
        failures.append(f"top_slot_score should be positive; got {out['top_slot_score']}")
    return _report("prefer_match_boosts_slot", failures)


def case_exclude_match_drops_slot() -> bool:
    """'월요일 안돼' → Monday slot is excluded entirely from selection."""
    convo = _convo(
        "이번 주 모이자",
        "월요일 안돼",
        "그럼 다른 날 어때?",
        "금요일 저녁 좋아",
    )
    out = select_guaranteed_slots(convo, DEFAULT_CANDIDATE_POOL, seed=7)
    failures = []
    if "월" not in out["blocked_weekdays"]:
        failures.append(f"월 not in blocked_weekdays: {out['blocked_weekdays']}")
    # Monday slot must NOT be selected
    for s in out["selected"]:
        if datetime.fromisoformat(s["start"]).weekday() == 0:
            failures.append(f"Monday slot snuck into selected: {s}")
    # Friday should still win (since it's strongly preferred and not blocked)
    if out["selected"]:
        top_wd = datetime.fromisoformat(out["selected"][0]["start"]).weekday()
        if top_wd != 4:
            failures.append(f"top should be 금 given prefer signal; got wd={top_wd}")
    return _report("exclude_match_drops_slot", failures)


def case_weekday_beats_daypart_when_specific() -> bool:
    """Per the (γ) spec: a single '금요일' weekday mention should beat
    multiple '저녁' daypart mentions (weekday=+3 vs daypart=+1 × N).
    Test convo: 1 weekday mention + 5 daypart mentions."""
    convo = _convo(
        "금요일에 다같이 모이자",       # weekday "금요일" × 1
        "저녁이면 좋겠는데",             # daypart "저녁" × 1
        "ㅇㅋ 저녁 좋아",                # daypart "저녁" × 1
        "저녁 7시쯤 어때",               # daypart "저녁" × 1
        "다들 저녁 8시 ok?",             # daypart "저녁" × 1
        "저녁 9시까지는 가능",            # daypart "저녁" × 1
    )
    out = select_guaranteed_slots(convo, DEFAULT_CANDIDATE_POOL, seed=42)
    failures = []
    if not out["selected"]:
        failures.append("selected is empty")
        return _report("weekday_beats_daypart_when_specific", failures)
    top_wd = datetime.fromisoformat(out["selected"][0]["start"]).weekday()
    if top_wd != 4:   # 금
        failures.append(
            f"weekday-specific should beat daypart: got wd={top_wd}, "
            f"top_score={out['top_slot_score']}"
        )
    # Breakdown should contain both weekday and daypart contributions
    if out["selected_breakdowns"]:
        bd = out["selected_breakdowns"][0]
        has_weekday = any("금요일" in b for b in bd)
        if not has_weekday:
            failures.append(f"breakdown missing weekday line: {bd}")
    return _report("weekday_beats_daypart_when_specific", failures)


def case_consensus_matches_scoring_data_source() -> bool:
    """Consensus uses raw text + same weight rules as scoring → for a
    conversation with multiple '금요일' mentions and no blocking exclude,
    consensus must return 금 (4). Also: the Top-1 selected slot's weekday
    should equal the consensus (self-consistency).
    """
    from data.select_guaranteed import consensus_weekday
    convo = _convo(
        "이번주 금요일 보자",
        "금요일 저녁 좋아",
        "5월 15일 금요일 7시 어때?",
        "금요일에 회식하자",
    )
    cons = consensus_weekday(convo)
    failures = []
    if cons != 4:
        failures.append(f"consensus should be 금(4), got {cons}")
    out = select_guaranteed_slots(convo, DEFAULT_CANDIDATE_POOL, seed=42)
    if out["selected"]:
        top_wd = datetime.fromisoformat(out["selected"][0]["start"]).weekday()
        if top_wd != cons:
            failures.append(
                f"data-source mismatch: consensus={cons} but Top-1 wd={top_wd} "
                "(should be self-consistent)"
            )
    return _report("consensus_matches_scoring_data_source", failures)


def case_seed_randomness_preserved() -> bool:
    """Same (conversation, seed) → identical selection (byte-equal)."""
    convo = _convo("주말 점심에 보자", "주말 좋아", "토요일 점심쯤 어때")
    a = select_guaranteed_slots(convo, DEFAULT_CANDIDATE_POOL, seed=99)
    b = select_guaranteed_slots(convo, DEFAULT_CANDIDATE_POOL, seed=99)
    failures = []
    if a["selected"] != b["selected"]:
        failures.append("same seed produced different selected lists")
    # Different seed should usually produce different result (sanity)
    c = select_guaranteed_slots(convo, DEFAULT_CANDIDATE_POOL, seed=100)
    # top slot is deterministic regardless of seed (highest score),
    # so verify the 2nd+ slot differs OR equality is fine if pool exhausted.
    if a["selected"] == c["selected"] and len(a["selected"]) > 1 and len(c["selected"]) > 1:
        # Allow this only when pool has 1 element after filtering (unlikely here).
        if a["pool_size_after"] > 2:
            failures.append("different seeds produced identical selections (unlikely)")
    return _report("seed_randomness_preserved", failures)


def main() -> int:
    cases = [
        case_exclude_weekday_filtered,
        case_exclude_weekend_filtered,
        case_pool_underflow_handled,
        case_false_positive_guard,
        case_prefer_match_boosts_slot,
        case_exclude_match_drops_slot,
        case_weekday_beats_daypart_when_specific,
        case_consensus_matches_scoring_data_source,
        case_seed_randomness_preserved,
    ]
    results = [c() for c in cases]
    passed = sum(results)
    print(f"\nSUMMARY: {passed}/{len(results)} select_guaranteed cases passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
