"""Conversation-aware filter for the guaranteed-slot pool.

When we hand ``generate_calendar`` a list of ``guaranteed_slots`` for the
non-trap case, those slots must not be the very ones the conversation
explicitly excluded — otherwise the labeler hits a contradiction
("calendar says Saturday is available, but the chat says Saturday is
out").

The filter is deterministic regex + day-of-week math. Patterns are kept
conservative to avoid the "토요일 어려운데 갈게" reversal trap: ``어려`` is
omitted from the exclude markers because Korean follow-up clauses ("갈게",
"괜찮아") often reverse it.

API:
    result = select_guaranteed_slots(
        conversation: list[{user, text, ts}],
        candidate_pool: list[{start, end}],
        seed: int,
        target_count: int = 3,
    )
    -> {
        "selected":              list[dict],
        "filtered_out":          list[dict],
        "blocked_weekdays":      list[str],   # ['월', '금', ...]
        "block_weekend":         bool,
        "pool_size_after":       int,
        "actual_count":          int,         # ≤ target_count
        "auto_trap_flip":        bool,        # True iff filtered pool is empty
    }
"""

from __future__ import annotations

import random
import re
from datetime import datetime
from typing import Any


WEEKDAY_MAP = {"월": 0, "화": 1, "수": 2, "목": 3, "금": 4, "토": 5, "일": 6}
WEEKDAY_NAMES = {v: k for k, v in WEEKDAY_MAP.items()}

# Exclude markers — conservative.
#   * "어려" omitted: reversal trap ("토요일 어려운데 갈게" — accept anyway).
#   * "없"/"없어" omitted: false-positive trap ("계획 없어" near 주말 wrongly
#     blocks the weekend, observed on p21). Cost is missing "그날 시간 없어"
#     style explicit excludes, which are rare and usually paired with
#     "안돼"/"못 가" anyway.
EXCLUDE_TAILS_RE = re.compile(
    r"(안돼|안 되|안되|빼고|빼|불가|못\s*(가|와|해)|싫|곤란|바빠)"
)
WEEKDAY_TOKEN_RE = re.compile(r"(월|화|수|목|금|토|일)요일")
WEEKEND_TOKEN_RE = re.compile(r"주말")
# "평일만/평일밖에/평일 아니면" — excludes weekend
WEEKDAY_ONLY_RE = re.compile(r"평일\s*(만\b|밖에|아니면)")
# Absolute date mentions like "5월 15일" / "5/15"
ABS_DATE_RE = re.compile(r"(\d{1,2})\s*월\s*(\d{1,2})\s*일")


# Score weights — controllable from the harness. Default is "1차" per the
# C4b (γ) spec. The tune-and-redraft harness escalates to (2차/3차) if
# alignment on p01~p05 stays below the 4/5 threshold.
WEIGHTS_DEFAULT: dict[str, int] = {
    "weekday":       3,   # "금요일" 등 명시 요일 매칭
    "weekend_token": 2,   # "주말" 토큰 매칭 (토·일 슬롯)
    "daypart":       1,   # "저녁" / "오후" / "점심" 등 시간대
    "absolute_date": 5,   # "5월 15일" 등 절대 날짜 매칭
    "exclude":       -3,  # blocked_days에 들어간 weekday에 거듭 패널티
}
WEIGHTS_LADDER: list[dict[str, int]] = [
    {"weekday": 3, "weekend_token": 2, "daypart": 1, "absolute_date": 5, "exclude": -3},
    {"weekday": 5, "weekend_token": 2, "daypart": 1, "absolute_date": 5, "exclude": -3},
    {"weekday": 5, "weekend_token": 2, "daypart": 0, "absolute_date": 5, "exclude": -3},
]

# How many characters back from an exclude marker do we look for a weekday/
# weekend token? 25 chars covers "월요일이랑 화요일은 안돼" (월요일 pos 0,
# 안돼 pos ~12) plus a margin.
EXCLUDE_LOOKBACK = 25


def _slot_weekday(slot: dict) -> int:
    return datetime.fromisoformat(slot["start"]).weekday()


def _find_excluded_days(text: str) -> tuple[set[int], bool]:
    """Return (blocked_weekday_ints, block_weekend).

    Strategy: scan the text for exclude markers, then for each marker look
    backwards in a 25-char window for weekday or 주말 tokens. Every token
    found in that window is treated as excluded. This lets one marker
    span multiple weekday mentions ('월요일이랑 화요일은 안돼').
    """
    blocked: set[int] = set()
    block_weekend = False
    for ex in EXCLUDE_TAILS_RE.finditer(text):
        window_lo = max(0, ex.start() - EXCLUDE_LOOKBACK)
        window = text[window_lo:ex.start()]
        for m in WEEKDAY_TOKEN_RE.finditer(window):
            blocked.add(WEEKDAY_MAP[m.group(1)])
        if WEEKEND_TOKEN_RE.search(window):
            block_weekend = True
    return blocked, block_weekend


def _score_slot_vs_conversation(
    slot: dict,
    text: str,
    blocked_days: set[int],
    block_weekend: bool,
    weights: dict[str, int] | None = None,
) -> tuple[int, list[str]]:
    """Alignment score + per-component breakdown.

    Differentiated weights (per the C4b γ spec; defaults in ``WEIGHTS_DEFAULT``):
        * weekday-specific token ("금요일")    → +W.weekday   (strong)
        * 주말 token (Sat/Sun slot only)        → +W.weekend_token
        * daypart keyword ("저녁"/"점심"/...)   → +W.daypart   (weak)
        * absolute date ("5월 15일")             → +W.absolute_date (strongest)
        * blocked-day penalty                   → +W.exclude   (negative)

    Returns ``(score, breakdown)``. ``breakdown`` is a list of human-readable
    "tag: ±N" strings for review-time inspection.
    """
    w = weights if weights is not None else WEIGHTS_DEFAULT
    s = datetime.fromisoformat(slot["start"])
    wd = s.weekday()
    h = s.hour
    score = 0
    breakdown: list[str] = []

    # Weekday mention match (e.g., '금요일')
    weekday_token = WEEKDAY_NAMES[wd] + "요"
    weekday_mentions = len(re.findall(weekday_token, text))
    if wd in blocked_days:
        pen = w["exclude"] * max(1, weekday_mentions)
        score += pen
        breakdown.append(
            f"weekday '{WEEKDAY_NAMES[wd]}요일' in blocked_days × {max(1, weekday_mentions)}: {pen:+d}"
        )
    elif weekday_mentions:
        gain = w["weekday"] * weekday_mentions
        score += gain
        breakdown.append(
            f"weekday '{WEEKDAY_NAMES[wd]}요일' × {weekday_mentions}: {gain:+d}"
        )

    # Relative-day tokens — boost the corresponding weekday slot.
    # Reference date is 2026-05-11 (Monday). 오늘 = Mon, 내일 = Tue, 모레 = Wed.
    REL_DAY_OFFSETS = {"오늘": 0, "내일": 1, "모레": 2}
    REF_WEEKDAY = 0
    for token, offset in REL_DAY_OFFSETS.items():
        if (REF_WEEKDAY + offset) % 7 != wd:
            continue
        # Count token occurrences, but only where it's not preceded by
        # 이번/다음 (those convert into 이번주/다음주 X요일 semantics).
        cnt = 0
        for m in re.finditer(token, text):
            lo = max(0, m.start() - 4)
            pre = text[lo:m.start()]
            if "이번" in pre or "다음" in pre:
                continue
            cnt += 1
        if cnt:
            if wd in blocked_days:
                pen = w["exclude"] * cnt
                score += pen
                breakdown.append(f"rel-day '{token}' (blocked) × {cnt}: {pen:+d}")
            else:
                gain = w["weekday"] * cnt
                score += gain
                breakdown.append(f"rel-day '{token}' × {cnt}: {gain:+d}")

    # Weekend token match for Sat/Sun slots
    if wd >= 5:
        weekend_mentions = len(WEEKEND_TOKEN_RE.findall(text))
        if block_weekend and weekend_mentions:
            pen = w["exclude"] * max(1, weekend_mentions)
            score += pen
            breakdown.append(f"weekend blocked × {max(1, weekend_mentions)}: {pen:+d}")
        elif weekend_mentions:
            gain = w["weekend_token"] * weekend_mentions
            score += gain
            breakdown.append(f"'주말' × {weekend_mentions}: {gain:+d}")

    # Absolute date match (e.g., '5월 15일')
    for m in ABS_DATE_RE.finditer(text):
        if int(m.group(1)) == s.month and int(m.group(2)) == s.day:
            score += w["absolute_date"]
            breakdown.append(
                f"absolute date '{m.group(1)}월{m.group(2)}일': {w['absolute_date']:+d}"
            )

    # Daypart matches (weakest signal)
    if w["daypart"] != 0:
        for label, hr_lo, hr_hi in (
            ("저녁", 18, 22), ("점심", 11, 14), ("오후", 13, 18), ("오전", 9, 12),
        ):
            if hr_lo <= h < hr_hi:
                cnt = text.count(label)
                if cnt:
                    gain = w["daypart"] * cnt
                    score += gain
                    breakdown.append(f"daypart '{label}' × {cnt}: {gain:+d}")
        # '낮' as a half-weight bonus paired with 점심 bucket
        if 11 <= h < 14:
            cnt = text.count("낮")
            if cnt:
                gain = max(1, w["daypart"]) * cnt
                # Only emit if it actually adds something
                if gain:
                    score += gain
                    breakdown.append(f"daypart '낮' × {cnt}: {gain:+d}")

    return score, breakdown


def consensus_weekday(
    conversation: list[dict],
    weights: dict[str, int] | None = None,
) -> int | None:
    """Detect the conversation's consensus weekday from raw text.

    Uses the SAME data source and weight rules as
    ``_score_slot_vs_conversation`` so the alignment measurement is
    self-consistent. Signals counted:
        * X요일 tokens         → +W.weekday per mention (or +W.exclude if blocked)
        * 주말 tokens           → +W.weekend_token to both Sat and Sun
                                  (or +W.exclude if blocked)
        * 평일만 (excludes weekend) → contributes the exclude penalty to Sat+Sun
        * 오늘/내일/모레 (with no "이번"/"다음" prefix) → +W.weekday on
          (reference_weekday + offset)

    Returns the unique-mode weekday integer (0=Mon..6=Sun), or None when
    no positive-score weekday exists OR multiple weekdays tie at the top.
    """
    w = weights if weights is not None else WEIGHTS_DEFAULT
    text = "\n".join(m.get("text", "") for m in conversation)
    blocked_days, block_weekend = _find_excluded_days(text)
    if WEEKDAY_ONLY_RE.search(text):
        block_weekend = True

    score: dict[int, int] = {}

    def _bump(wd: int, delta: int) -> None:
        score[wd] = score.get(wd, 0) + delta

    # X요일 mentions
    for m in WEEKDAY_TOKEN_RE.finditer(text):
        wd = WEEKDAY_MAP[m.group(1)]
        _bump(wd, w["exclude"] if wd in blocked_days else w["weekday"])

    # 주말 contributes to both Sat and Sun
    for _ in WEEKEND_TOKEN_RE.finditer(text):
        if block_weekend:
            _bump(5, w["exclude"])
            _bump(6, w["exclude"])
        else:
            _bump(5, w["weekend_token"])
            _bump(6, w["weekend_token"])

    # Relative-day tokens
    REL = {"오늘": 0, "내일": 1, "모레": 2}
    REF_WEEKDAY = 0   # 2026-05-11 == Monday
    for token, offset in REL.items():
        wd = (REF_WEEKDAY + offset) % 7
        for m in re.finditer(token, text):
            pre = text[max(0, m.start() - 4):m.start()]
            if "이번" in pre or "다음" in pre:
                continue
            _bump(wd, w["exclude"] if wd in blocked_days else w["weekday"])

    # Drop non-positive scores; only positive consensus counts
    positive = {wd: s for wd, s in score.items() if s > 0}
    if not positive:
        return None
    top_score = max(positive.values())
    winners = [wd for wd, s in positive.items() if s == top_score]
    if len(winners) > 1:
        return None
    return winners[0]


def select_guaranteed_slots(
    conversation: list[dict],
    candidate_pool: list[dict],
    seed: int,
    target_count: int = 3,
    weights: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Pick guaranteed slots biased toward the conversation's agreement.

    Algorithm:
        1. Filter out slots on excluded weekdays (existing).
        2. Score each remaining slot by alignment with prefer signals in
           the conversation (see ``_score_slot_vs_conversation``).
        3. The single highest-scoring slot is **always** included (the
           "정답 보장" slot). If all scores are ≤ 0, fall back to
           lowest-index pool slot.
        4. The remaining selection (count-1 more slots) is drawn from the
           rest via seed-shuffled random, biased to 2~3 total.

    Determinism: identical (conversation, candidate_pool, seed) → identical
    output. Top-score tie-break by candidate_pool index (stable).
    """
    text = "\n".join(m.get("text", "") for m in conversation)

    blocked_days, block_weekend = _find_excluded_days(text)
    if WEEKDAY_ONLY_RE.search(text):
        block_weekend = True
    if block_weekend:
        blocked_days |= {5, 6}

    filtered_in: list[dict] = []
    filtered_out: list[dict] = []
    for slot in candidate_pool:
        wd = _slot_weekday(slot)
        if wd in blocked_days:
            filtered_out.append(slot)
        else:
            filtered_in.append(slot)

    # Score each surviving slot (with breakdown for review-time inspection)
    scored = []
    for idx, slot in enumerate(filtered_in):
        sc, bd = _score_slot_vs_conversation(
            slot, text, blocked_days, block_weekend, weights=weights,
        )
        scored.append((idx, slot, sc, bd))
    # Pre-shuffle by seed so tied scores break by seed-determined order,
    # not by pool index (which would always favor Monday — observed
    # producing 47% Monday-dominance pre-fix). Stable sort then groups
    # ties together while preserving the shuffled order within each group.
    rng = random.Random(seed)
    rng.shuffle(scored)
    scored_sorted = sorted(scored, key=lambda x: -x[2])

    selected: list[dict] = []
    selected_breakdowns: list[list[str]] = []
    selected_scores: list[int] = []
    top_slot_score = 0
    if scored_sorted:
        top_slot_score = scored_sorted[0][2]
        selected.append(scored_sorted[0][1])
        selected_breakdowns.append(scored_sorted[0][3])
        selected_scores.append(scored_sorted[0][2])

    # Decide total target count (2 or 3, with seed-based bias)
    if len(filtered_in) >= 2:
        actual_count = rng.choice([target_count, max(2, target_count - 1)])
    else:
        actual_count = min(target_count, len(filtered_in))

    rest = [(s, sc, bd) for _, s, sc, bd in scored_sorted[1:]]
    rng.shuffle(rest)
    for s, sc, bd in rest[:max(0, actual_count - len(selected))]:
        selected.append(s)
        selected_breakdowns.append(bd)
        selected_scores.append(sc)

    return {
        "selected": selected,
        "selected_breakdowns": selected_breakdowns,
        "selected_scores": selected_scores,
        "filtered_out": filtered_out,
        "blocked_weekdays": sorted(WEEKDAY_NAMES[d] for d in blocked_days),
        "block_weekend": block_weekend,
        "pool_size_after": len(filtered_in),
        "actual_count": len(selected),
        "auto_trap_flip": (len(selected) == 0),
        "top_slot_score": top_slot_score,
        "all_scores": [(s["start"], sc) for _, s, sc, _ in scored],
        "weights": weights if weights is not None else WEIGHTS_DEFAULT,
    }


# Default candidate pool for the 14-day window 2026-05-04 .. 2026-05-17.
# Weekday evenings + weekend afternoons/evenings — typical meeting slots.
DEFAULT_CANDIDATE_POOL: list[dict] = [
    {"start": "2026-05-11T19:00", "end": "2026-05-11T21:00"},  # 월
    {"start": "2026-05-12T19:00", "end": "2026-05-12T21:00"},  # 화
    {"start": "2026-05-13T19:00", "end": "2026-05-13T21:00"},  # 수
    {"start": "2026-05-14T19:00", "end": "2026-05-14T21:00"},  # 목
    {"start": "2026-05-15T19:00", "end": "2026-05-15T21:00"},  # 금
    {"start": "2026-05-16T14:00", "end": "2026-05-16T16:00"},  # 토 오후
    {"start": "2026-05-16T19:00", "end": "2026-05-16T21:00"},  # 토 저녁
    {"start": "2026-05-17T14:00", "end": "2026-05-17T16:00"},  # 일 오후
    {"start": "2026-05-17T19:00", "end": "2026-05-17T21:00"},  # 일 저녁
]
