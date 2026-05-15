"""Auto-draft ``expected_*`` fields for the golden_30 dataset.

Strategy: use the Solar-only step1+step2_normalize path (the same path
the measurement pipeline falls back to when IE fails) to extract per-row
preferences from each conversation. This gives an independent draft that
the IE+Solar measurement path can be meaningfully compared against.

Per-field defaults:
    expected_extracted  — deduplicated (who, time_expr_raw, evidence_msg_id)
                          rows from step2_normalize output. start/end and
                          certainty are dropped (not part of the
                          public 5-field contract).
    expected_top3       — guaranteed_slots from calendar_meta that survive
                          the actual calendar intersection (drops the
                          slot that the trap mechanism blocked). Up to 3.
    expected_unresolved — empty by default; the 5 calendar-trap scenarios
                          also get [] because the trap is calendar-side
                          (no extraction-level hallucination). A reviewer
                          may add entries if the conversation contains a
                          subtle hallucination cue.

Output: assets/golden/golden_30.draft.jsonl
        (the original golden_30.jsonl is left untouched.)

CLI:
    python generate_draft_labels.py --only p01,p02,p03,p04,p05   # first 5
    python generate_draft_labels.py --all
    python generate_draft_labels.py --review-md                  # regenerate the human review file
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from data.generate_calendar import intersection_30min
from pipeline_ie_mode import _solar_path
from upstage_client import UpstageClient


SKILL_ROOT = Path(__file__).resolve().parent.parent
GOLDEN_DIR = SKILL_ROOT / "assets" / "golden"
GOLDEN_PATH = GOLDEN_DIR / "golden_30.jsonl"
DRAFT_PATH = GOLDEN_DIR / "golden_30.draft.jsonl"
REVIEW_PATH = GOLDEN_DIR / "golden_30.draft.review.md"

REFERENCE_DATE = "2026-05-11"
WEEKDAY_KR = ["월", "화", "수", "목", "금", "토", "일"]


def _parse(s: str) -> datetime:
    return datetime.fromisoformat(s)


# ---- extracted-preferences draft -------------------------------------------

# Time-keyword whitelist: at least one of these must appear in time_expr_raw
# for a row to survive the C4b Option-A filter. Without a time-anchored token
# the phrase is almost certainly an anaphora ('그때') or ack ('오케이').
import re as _re

TIME_KEYWORD_RE = _re.compile(
    r"(월|화|수|목|금|토|일)요일"           # 월요일~일요일
    r"|월요?일|화요?일|수요?일|목요?일|금요?일|토요?일|일요?일"
    r"|주말|평일|이번주|다음주|이번 주|다음 주|"
    r"|이번주말|다음주말|내일|모레|오늘|어제|"
    r"|오전|오후|저녁|아침|점심|새벽|낮|밤|"
    r"|이른|늦게|적당히|쯤|"
    r"|\d+시|\d+:\d+|\d+분|\d+월\s*\d+일"  # 7시, 7:30, 30분, 5월 17일
)
# Anaphora & filler patterns that must never survive even if they include a keyword
ANAPHORA_RE = _re.compile(r"^(그때|이때|그날|그러면|그럼)$")
ACK_PHRASES = {"오케이", "ok", "OK", "ㅇㅋ", "ㄱㄱ", "네", "넵", "응", "응응", "그래", "콜"}


def _is_time_anchored(phrase: str) -> bool:
    p = (phrase or "").strip()
    if not p:
        return False
    if p in ACK_PHRASES:
        return False
    if ANAPHORA_RE.search(p):
        return False
    # Pure ack tokens with punctuation (e.g., "오케이~")
    bare = _re.sub(r"[\s~ㅋㅎ!?.,]+", "", p)
    if bare in ACK_PHRASES:
        return False
    return bool(TIME_KEYWORD_RE.search(p))


def _dedupe_extracted(items: list[dict]) -> list[dict]:
    """Collapse multi-day-expanded rows to one row per (who, phrase, msg_id).

    step2_normalize emits one row per (day, time-window) for a single phrase
    like "다음주 점심쯤". For the public ``expected_extracted`` contract we
    want phrase-level granularity, not day-level. Dedup key:
    (who, time_expr_raw, evidence_msg_id).

    Option-A filter: drop rows whose time_expr_raw is not time-anchored
    (anaphora, simple acks, non-time content). This reduces labeler review
    burden — see the C4b decision log.
    """
    seen: set[tuple[Any, ...]] = set()
    out: list[dict] = []
    for it in items:
        phrase = it.get("time")
        if not _is_time_anchored(phrase):
            continue
        key = (it.get("who"), phrase, it.get("evidence_msg_id"))
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "who": it.get("who"),
            "type": it.get("type"),
            "time_expr_raw": phrase,
            "evidence_msg_id": it.get("evidence_msg_id"),
        })
    # Sort by evidence_msg_id then who for stable diff
    out.sort(key=lambda r: (r.get("evidence_msg_id") or 0, r.get("who") or ""))
    return out


def _extract_via_solar(client: UpstageClient, conversation: list[dict]) -> list[dict]:
    """Solar step1+step2 with retry on transient timeouts.

    Long conversations + ``reasoning_effort=medium`` can overrun the 60s
    default Solar timeout. Retry up to 3× with progressive backoff so a
    single slow request doesn't kill the whole draft run.
    """
    import time as _time
    import requests as _requests

    last: Exception | None = None
    for attempt in range(3):
        try:
            step2 = _solar_path(client, conversation, REFERENCE_DATE)
            return _dedupe_extracted(step2.get("items", []))
        except (_requests.ReadTimeout, _requests.ConnectionError) as e:
            last = e
            _time.sleep(3 * (2 ** attempt))
    raise last  # type: ignore[misc]


# ---- Top-3 draft ----------------------------------------------------------

def _slot_30min_set(start: str, end: str) -> set[str]:
    s = _parse(start)
    e = _parse(end)
    out: set[str] = set()
    t = s
    while t < e:
        out.add(t.strftime("%Y-%m-%dT%H:%M"))
        t += timedelta(minutes=30)
    return out


def _slot_keyword_score(slot: dict, extracted: list[dict]) -> tuple[int, list[str]]:
    """Score a guaranteed slot by its alignment with conversation prefer rows.

    Score = number of extracted prefer rows whose time_expr_raw mentions
    the same weekday or daypart as the slot. Returns (score, supporting_phrases).

    Mapping rules:
      * slot 월요일 ↔ "월요일", "월", "월요"
      * 18:00-21:00 ↔ "저녁"
      * 11:00-14:00 ↔ "점심", "낮"
      * 14:00-18:00 ↔ "오후"
      * 09:00-12:00 ↔ "오전"
      * 20:00+      ↔ "늦게", "밤"
    """
    s = _parse(slot["start"])
    e = _parse(slot["end"])
    weekday_token = WEEKDAY_KR[s.weekday()] + "요일"

    # Daypart token by start hour
    dayparts: list[str] = []
    h = s.hour
    if 9 <= h < 12:
        dayparts.append("오전")
    if 11 <= h < 14:
        dayparts += ["점심", "낮"]
    if 13 <= h < 18:
        dayparts.append("오후")
    if 18 <= h < 22:
        dayparts.append("저녁")
    if h >= 20 or h < 6:
        dayparts.append("늦게")
    if 5 <= s.weekday() <= 6:
        dayparts.append("주말")
    if s.weekday() < 5:
        dayparts.append("평일")

    score = 0
    supporters: list[str] = []
    for r in extracted:
        if r.get("type") != "prefer":
            continue
        phrase = r.get("time_expr_raw") or ""
        hit = False
        if weekday_token in phrase or WEEKDAY_KR[s.weekday()] + "요" in phrase:
            hit = True
        else:
            for d in dayparts:
                if d in phrase:
                    hit = True
                    break
        if hit:
            score += 1
            supporters.append(f"{r['who']}/'{phrase}'")
    return score, supporters


def _draft_top3(rec: dict, extracted: list[dict]) -> list[dict]:
    """Top-3 from guaranteed_slots in select_guaranteed's saved order.

    Algorithm:
      1. Iterate guaranteed_slots in order (already sorted by select score).
      2. Keep slots that fully survive the 30-min intersection.
      3. Take up to 3.
      4. Rationale embeds the score breakdown from calendar_meta when
         available (so the reviewer sees *why* this slot was chosen).
    """
    meta = rec.get("calendar_meta", {}) or {}
    cal = rec.get("calendars") or {}
    guaranteed = meta.get("guaranteed_slots") or []
    scores = meta.get("guaranteed_scores") or [None] * len(guaranteed)
    breakdowns = meta.get("guaranteed_breakdowns") or [[]] * len(guaranteed)
    intersect = intersection_30min(cal)
    participants = sorted(rec["scenario"]["people"])

    out: list[dict] = []
    for slot, score, bd in zip(guaranteed, scores, breakdowns):
        if len(out) >= 3:
            break
        slot_set = _slot_30min_set(slot["start"], slot["end"])
        if not slot_set.issubset(intersect):
            continue
        s = _parse(slot["start"])
        e = _parse(slot["end"])
        wd = WEEKDAY_KR[s.weekday()]
        head = (
            f"{s.strftime('%m/%d')}({wd}) "
            f"{s.strftime('%H:%M')}~{e.strftime('%H:%M')}"
        )
        # Compose rationale: score header + breakdown lines + supporter
        # phrases from extracted (for context).
        sup = _slot_keyword_score(slot, extracted)[1]
        parts = [head]
        if score is not None:
            parts.append(f"[score={score}]")
        if bd:
            parts.append("  · " + "  · ".join(bd))
        if sup:
            parts.append(
                f"  prefer phrases: {', '.join(sup[:3])}{' …' if len(sup) > 3 else ''}"
            )
        parts.append("  (모든 참여자 캘린더 해당 슬롯 비어있음)")
        rationale = "\n".join(parts)
        out.append({
            "start": slot["start"],
            "end": slot["end"],
            "participants_available": participants,
            "rationale": rationale,
            "score": score,
        })
    return out


# ---- unresolved draft -----------------------------------------------------

def _draft_unresolved(rec: dict) -> list[dict]:
    """Auto-insert an entry for trap scenarios.

    The 5 traps are calendar-side (engineered intersection shortage). To give
    M5 measurable signal we record one ``(who="*", time_expr_raw=<blocked-
    slot label>)`` entry per trap so the evaluator can check whether the
    pipeline emits a matching unresolved marker. Non-trap scenarios stay [].
    """
    meta = rec.get("calendar_meta", {}) or {}
    if not meta.get("is_trap_effective"):
        return []

    guaranteed = meta.get("guaranteed_slots") or []
    cal = rec.get("calendars") or {}
    intersect = intersection_30min(cal)

    blocked = []
    for slot in guaranteed:
        slot_set = _slot_30min_set(slot["start"], slot["end"])
        if not slot_set.issubset(intersect):
            blocked.append(slot)

    if not blocked:
        # Trap meta says trap=True but every slot survived — shouldn't happen
        # under our generator, but fail safe with a generic note rather than
        # silently emitting an empty unresolved.
        return [{
            "who": "*",
            "time_expr_raw": "(blocked slot indeterminate)",
            "reason": "trap=true이나 모든 guaranteed_slot이 교집합에 생존 — 데이터셋 점검 필요",
        }]

    s = _parse(blocked[0]["start"])
    e = _parse(blocked[0]["end"])
    wd = WEEKDAY_KR[s.weekday()]
    label = f"{s.strftime('%m/%d')}({wd}) {s.strftime('%H:%M')}~{e.strftime('%H:%M')}"
    return [{
        "who": "*",
        "time_expr_raw": label,
        "reason": "캘린더 교집합 부족 — 1명 이상의 참여자가 해당 시간 불가",
    }]


# ---- driver --------------------------------------------------------------

def _load_records() -> list[dict]:
    return [json.loads(l) for l in GOLDEN_PATH.read_text().splitlines()]


def _load_draft() -> list[dict]:
    if DRAFT_PATH.exists():
        return [json.loads(l) for l in DRAFT_PATH.read_text().splitlines()]
    return _load_records()   # first run: seed from original


def _save_draft(records: list[dict]) -> None:
    DRAFT_PATH.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n"
    )


def draft_one(client: UpstageClient, rec: dict) -> dict:
    extracted = _extract_via_solar(client, rec["scenario"]["conversation"])
    top3 = _draft_top3(rec, extracted)
    unresolved = _draft_unresolved(rec)
    rec = dict(rec)   # shallow copy so caller's record stays clean
    rec["expected_extracted"] = extracted
    rec["expected_top3"] = top3
    rec["expected_unresolved"] = unresolved
    return rec


def _render_review(records: list[dict], scope_ids: set[str] | None = None) -> str:
    lines: list[str] = []
    lines.append(f"# golden_30 — draft labels review (reference_date = {REFERENCE_DATE})")
    lines.append("")
    lines.append("각 시나리오의 자동 초안을 훑으며 명백히 틀린 부분만 메모해주세요.")
    lines.append("수정 사항은 free-form text로 해당 섹션 하단에 적어주시면, ")
    lines.append("Claude Code가 jsonl에 반영합니다.")
    lines.append("")
    for rec in records:
        sid = rec["scenario_id"]
        if scope_ids is not None and sid not in scope_ids:
            continue
        spec = rec["spec"]
        meta = rec.get("calendar_meta", {})
        density = meta.get("density", "?")
        trap = meta.get("is_trap_effective", False)
        lines.append(f"## {sid}  ({spec['topic']}, n={spec['people_n']}, "
                     f"density={density}, trap={'true' if trap else 'false'})")
        lines.append("")
        lines.append("**Conversation**")
        lines.append("")
        for i, m in enumerate(rec["scenario"]["conversation"], 1):
            lines.append(f"[{i}] {m['user']} ({m.get('ts','')}): {m['text']}")
        lines.append("")
        ex = rec.get("expected_extracted", [])
        lines.append(f"**Draft expected_extracted ({len(ex)}건)**")
        lines.append("")
        for i, e in enumerate(ex, 1):
            lines.append(
                f"{i}. {e['who']} / {e['type']} / \"{e['time_expr_raw']}\" "
                f"/ msg_{e['evidence_msg_id']}"
            )
        if not ex:
            lines.append("_(없음)_")
        lines.append("")
        top3 = rec.get("expected_top3", [])
        lines.append(f"**Draft expected_top3 ({len(top3)}건)**")
        lines.append("")
        for i, s in enumerate(top3, 1):
            ts = _parse(s["start"])
            te = _parse(s["end"])
            wd = WEEKDAY_KR[ts.weekday()]
            avail = ", ".join(s["participants_available"])
            lines.append(
                f"{i}. {ts.strftime('%m/%d')}({wd}) "
                f"{ts.strftime('%H:%M')}~{te.strftime('%H:%M')} — "
                f"가능: {avail}"
            )
            lines.append(f"   사유: {s['rationale']}")
        if not top3:
            lines.append("_(없음 — 캘린더 교집합 부족)_")
        lines.append("")
        unr = rec.get("expected_unresolved", [])
        lines.append(f"**Draft expected_unresolved ({len(unr)}건)**")
        lines.append("")
        if unr:
            for i, u in enumerate(unr, 1):
                lines.append(f"{i}. {u}")
        else:
            lines.append("_(없음)_")
        lines.append("")
        lines.append("---")
        lines.append("**검토**")
        lines.append("- [ ] 모두 정확")
        lines.append("- [ ] 수정 필요: <메모>")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default=None,
                    help="comma-separated scenario_ids to draft (e.g. p01,p02)")
    ap.add_argument("--all", action="store_true",
                    help="draft every scenario in golden_30")
    ap.add_argument("--review-md", action="store_true",
                    help="regenerate review markdown from existing draft")
    args = ap.parse_args()

    records = _load_draft()
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)

    if args.review_md:
        REVIEW_PATH.write_text(_render_review(records))
        print(f"Wrote: {REVIEW_PATH}")
        return 0

    if not args.only and not args.all:
        print("Pass --only <ids> or --all")
        return 1
    target = (
        {x.strip() for x in args.only.split(",")} if args.only
        else {r["scenario_id"] for r in records}
    )
    client = UpstageClient()
    drafted = 0
    for i, rec in enumerate(records):
        if rec["scenario_id"] not in target:
            continue
        sid = rec["scenario_id"]
        print(f"  drafting {sid}…", flush=True)
        records[i] = draft_one(client, rec)
        drafted += 1
        _save_draft(records)   # incremental
    REVIEW_PATH.write_text(_render_review(records, target))
    print()
    print(f"Drafted: {drafted} scenarios")
    print(f"Wrote: {DRAFT_PATH}")
    print(f"Wrote: {REVIEW_PATH}  (filtered to {len(target)} scenarios)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
