"""C4a-cal-3 — batch calendar generation for all 30 golden scenarios.

For each scenario in golden_30.jsonl:
    1. Look up density and trap flag from synthesize_calendars.DENSITY_ASSIGNMENT
       (the original Solar-mode script — kept for assignment metadata only).
    2. Run select_guaranteed_slots on the conversation. If the filter
       auto-flips to trap (filtered pool empty), respect that.
    3. Call generate_calendar (deterministic) with density + guaranteed +
       trap flag.
    4. Compute the 30-min intersection and a per-participant total-free-h
       summary for the matrix report.
    5. Write the calendar into the JSONL record (overwriting any prior
       Solar-attempt remnants).

The write is incremental — partial progress survives a crash.
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

# Reuse assignment + trap metadata from the (now-abandoned) Solar script.
from synthesize_calendars import DENSITY_ASSIGNMENT, TRAP_IDS, JSONL_PATH
from data.generate_calendar import generate_calendar, intersection_30min
from data.select_guaranteed import (
    DEFAULT_CANDIDATE_POOL,
    WEIGHTS_DEFAULT,
    select_guaranteed_slots,
)


REFERENCE = "2026-05-11"


def _load_records() -> list[dict]:
    return [json.loads(l) for l in JSONL_PATH.read_text().splitlines()]


def _save_records(records: list[dict]) -> None:
    JSONL_PATH.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n"
    )


def _seed_for(scenario_id: str) -> int:
    return int(hashlib.sha256(f"{scenario_id}-guaranteed".encode()).hexdigest()[:8], 16)


def _per_participant_total_hours(cal: dict[str, list[dict]]) -> dict[str, float]:
    out = {}
    for p, ws in cal.items():
        total = sum(
            (datetime.fromisoformat(w["end"]) - datetime.fromisoformat(w["start"])).total_seconds() / 3600
            for w in ws
        )
        out[p] = round(total, 1)
    return out


def run(weights: dict[str, int] | None = None) -> tuple[list[dict], list[dict]]:
    """Regenerate calendars for all 30 scenarios with the given weights.

    Returns ``(summary_rows, records)`` so a harness can inspect alignment
    metrics without re-loading the JSONL. JSONL is saved as a side effect.
    """
    records = _load_records()

    summary_rows: list[dict[str, Any]] = []
    filter_active = 0   # filtered_out > 0
    auto_trap_count = 0

    for rec in records:
        sid = rec["scenario_id"]
        scenario = rec["scenario"]
        density = DENSITY_ASSIGNMENT[sid]
        trap_planned = sid in TRAP_IDS

        # 1. Conversation-aware filter (with γ-weight scoring)
        gs = select_guaranteed_slots(
            scenario["conversation"],
            DEFAULT_CANDIDATE_POOL,
            seed=_seed_for(sid),
            weights=weights,
        )
        if gs["filtered_out"]:
            filter_active += 1
        # auto_trap_flip overrides planned: if filter killed the pool, this
        # scenario becomes a trap whether or not it was originally one.
        is_trap = trap_planned or gs["auto_trap_flip"]
        if gs["auto_trap_flip"] and not trap_planned:
            auto_trap_count += 1

        # 2. Generate calendar
        try:
            cal = generate_calendar(
                sid,
                scenario["people"],
                REFERENCE,
                density,
                guaranteed_slots=gs["selected"],
                is_trap=is_trap,
            )
        except Exception as e:
            print(f"{sid}: FAIL {type(e).__name__}: {e}")
            continue

        inter = intersection_30min(cal)
        n_inter_slots = len(inter)
        per_p_hours = _per_participant_total_hours(cal)
        mean_hours = round(sum(per_p_hours.values()) / len(per_p_hours), 1)

        rec["calendars"] = cal
        rec["calendar_meta"] = {
            "density": density,
            "is_trap_planned": trap_planned,
            "is_trap_effective": is_trap,
            "auto_trap_flip": gs["auto_trap_flip"],
            "blocked_weekdays": gs["blocked_weekdays"],
            "block_weekend": gs["block_weekend"],
            "pool_size_after_filter": gs["pool_size_after"],
            "guaranteed_count": gs["actual_count"],
            "guaranteed_slots": gs["selected"],
            "guaranteed_scores": gs.get("selected_scores", []),
            "guaranteed_breakdowns": gs.get("selected_breakdowns", []),
            "intersect_30min_slots": n_inter_slots,
            "mean_free_hours_per_person": mean_hours,
            "weights": gs.get("weights"),
        }

        # Drop the obsolete Solar-attempt metadata if present.
        rec.pop("calendar_synth_meta", None)

        summary_rows.append({
            "id": sid, "density": density, "trap": is_trap,
            "auto_flip": gs["auto_trap_flip"],
            "guaranteed": gs["actual_count"],
            "blocked": gs["blocked_weekdays"] + (["주말"] if gs["block_weekend"] else []),
            "inter_slots": n_inter_slots,
            "mean_h": mean_hours,
        })
        _save_records(records)  # incremental

    # ── Matrix report ────────────────────────────────────────────────────
    print()
    print("=" * 86)
    print("Per-scenario summary")
    print("=" * 86)
    print(f"{'id':<5} {'dens':<5} {'trap':<6} {'auto':<6} {'gtd':<4} {'inter':<6} {'mean_h':<7} blocked")
    for r in summary_rows:
        trap_mark = "T" if r["trap"] else "F"
        auto_mark = "★" if r["auto_flip"] else " "
        blocked = ",".join(r["blocked"]) or "-"
        print(
            f"{r['id']:<5} {r['density']:<5} {trap_mark:<6} {auto_mark:<6} "
            f"{r['guaranteed']:<4} {r['inter_slots']:<6} {r['mean_h']:<7} {blocked}"
        )

    print()
    print("=" * 86)
    print("Matrix verification")
    print("=" * 86)
    density_counts = Counter(r["density"] for r in summary_rows)
    trap_effective = sum(1 for r in summary_rows if r["trap"])
    auto_flip_total = sum(1 for r in summary_rows if r["auto_flip"])
    gtd_counts = Counter(r["guaranteed"] for r in summary_rows)
    print(f"density: {dict(density_counts)}  (target 여유:5 / 보통:15 / 빡빡:10)")
    print(f"trap effective: {trap_effective}/30  (planned {len(TRAP_IDS)}/30; auto-flip extras: {auto_flip_total})")
    print(f"filter_active scenarios (some slots removed): {filter_active}/30")
    print(f"guaranteed_count distribution: {dict(sorted(gtd_counts.items()))}")
    print()

    # ── Diversity guard ─────────────────────────────────────────────────
    # Measure the weekday of expected_top3[0] (= guaranteed_slots[0], the
    # highest-scoring slot per the new prefer-aware selection). Warn if
    # any weekday dominates > 40% of scenarios — that indicates the
    # synthesis biased every scenario toward the same day, hurting
    # measurement diversity.
    WEEKDAY_KR = ["월", "화", "수", "목", "금", "토", "일"]
    top_weekdays = Counter()
    for rec in records:
        guaranteed = (rec.get("calendar_meta") or {}).get("guaranteed_slots") or []
        if not guaranteed:
            continue
        top = guaranteed[0]
        try:
            wd = datetime.fromisoformat(top["start"]).weekday()
        except (KeyError, ValueError):
            continue
        top_weekdays[WEEKDAY_KR[wd]] += 1

    total = sum(top_weekdays.values())
    print("=" * 86)
    print("Diversity guard — expected_top3[0] 요일 분포")
    print("=" * 86)
    if total == 0:
        print("(empty — no records have guaranteed_slots)")
    else:
        for wd in WEEKDAY_KR:
            cnt = top_weekdays.get(wd, 0)
            pct = cnt / total * 100
            bar = "▆" * cnt
            print(f"  {wd}요일: {cnt:>2}/{total} ({pct:>5.1f}%) {bar}")
        threshold = 0.4
        for wd, cnt in top_weekdays.most_common():
            if cnt / total > threshold:
                print()
                print(
                    f"  ⚠ {wd}요일 dominance: {cnt}/{total} = {cnt/total*100:.0f}% > {threshold*100:.0f}%"
                )
                print(
                    "    원본 시나리오 합성 단계에서 prefer 요일이 한 쪽으로 쏠려 있을 가능성."
                    " 단순 분산 시도는 미구현 — 그대로 진행 시 발표에서 사유 명시 필요."
                )
                break
        else:
            print()
            print("  ✅ no single weekday exceeds 40% — diversity OK")
    print()
    return summary_rows, records


def main() -> int:
    run()
    return 0

    # Sanity warnings
    if auto_flip_total > 5:
        print(f"⚠ auto_trap_flip = {auto_flip_total} (> 5) — exceeds expected upper bound. "
              "Conversation distribution may need review.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
