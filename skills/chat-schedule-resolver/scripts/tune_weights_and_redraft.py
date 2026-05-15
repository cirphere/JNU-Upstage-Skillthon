"""C4b (γ) — auto-tune the select_guaranteed weights against p01~p05.

Loop the WEIGHTS_LADDER (3 levels):
  1차: weekday=3 daypart=1
  2차: weekday=5 daypart=1
  3차: weekday=5 daypart=0

For each level:
  1. Regenerate all 30 calendars with that weight set.
  2. Rebuild p01~p05 expected_top3 + expected_unresolved from the new
     calendars (reusing existing expected_extracted — no Solar calls).
  3. Measure 'consensus weekday' from each scenario's expected_extracted
     (mode of weekday tokens in prefer rows). For p01~p05, compare against
     guaranteed_slots[0]'s weekday.
  4. Stop when matches ≥ 4/5; otherwise advance to the next level.

Exit code 0 if any level reaches the threshold; 1 if all 3 fail (data
synthesis itself is biased — see brief).
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

from data.select_guaranteed import WEIGHTS_LADDER, consensus_weekday
import generate_all_calendars as gac
import generate_draft_labels as gdl


SKILL_ROOT = Path(__file__).resolve().parent.parent
DRAFT_PATH = SKILL_ROOT / "assets" / "golden" / "golden_30.draft.jsonl"

REFERENCE_WEEKDAY = 0  # 2026-05-11 == Monday
WEEKDAY_KR = ["월", "화", "수", "목", "금", "토", "일"]
WEEKDAY_MAP = {w: i for i, w in enumerate(WEEKDAY_KR)}

SCOPE = ["p01", "p02", "p03", "p04", "p05"]


def _consensus_weekday(extracted: list[dict], conversation: list[dict] | None = None) -> int | None:
    """Bridge to the new raw-text-based ``consensus_weekday`` in
    ``data.select_guaranteed``. We keep this thin wrapper so callers can
    still pass ``extracted`` for backward compat, but the underlying
    consensus uses the conversation text (same data source as scoring).
    """
    if conversation is None:
        # Backward-compat path when only extracted is available (still
        # returns None for fully unknown cases).
        return None
    return consensus_weekday(conversation)


def _measure_alignment(records: list[dict]) -> tuple[int, int, list[str]]:
    """Return (matches, total_unambiguous, per_scenario_lines).

    ``records`` carries the up-to-date ``calendar_meta`` (just regenerated),
    but ``expected_extracted`` lives in the *draft* jsonl, so we read it
    from there. Match-up is by scenario_id.
    """
    if DRAFT_PATH.exists():
        draft_by_id = {
            r["scenario_id"]: r
            for r in (json.loads(l) for l in DRAFT_PATH.read_text().splitlines())
        }
    else:
        draft_by_id = {}

    matches = 0
    unambiguous = 0
    lines: list[str] = []
    by_id = {r["scenario_id"]: r for r in records}
    for sid in SCOPE:
        rec = by_id.get(sid)
        if not rec:
            lines.append(f"  {sid}: missing in dataset")
            continue
        # Consensus from raw conversation text (same data source as scoring).
        consensus_wd = consensus_weekday(rec["scenario"]["conversation"])
        gtd = (rec.get("calendar_meta") or {}).get("guaranteed_slots") or []
        if not gtd:
            lines.append(f"  {sid}: no guaranteed slot → △ skip")
            continue
        top_wd = datetime.fromisoformat(gtd[0]["start"]).weekday()
        if consensus_wd is None:
            lines.append(
                f"  {sid}: consensus = ambiguous, Top-1 = {WEEKDAY_KR[top_wd]}요일 → △"
            )
            continue
        unambiguous += 1
        if consensus_wd == top_wd:
            matches += 1
            mark = "✅"
        else:
            mark = "❌"
        lines.append(
            f"  {sid}: consensus '{WEEKDAY_KR[consensus_wd]}요일' vs Top-1 "
            f"'{WEEKDAY_KR[top_wd]}요일' → {mark}"
        )
    return matches, unambiguous, lines


def _rebuild_drafts_for_scope(records_full: list[dict]) -> None:
    """Recompute expected_top3 + expected_unresolved for SCOPE using the
    just-regenerated calendars. Preserves existing expected_extracted.
    """
    if not DRAFT_PATH.exists():
        print(f"  ⚠ draft jsonl missing: {DRAFT_PATH}")
        return
    draft = [json.loads(l) for l in DRAFT_PATH.read_text().splitlines()]
    golden_by_id = {r["scenario_id"]: r for r in records_full}
    for rec in draft:
        sid = rec["scenario_id"]
        if sid not in SCOPE:
            continue
        # Pull updated calendars/meta from the just-saved golden_30.jsonl
        g = golden_by_id.get(sid)
        if not g:
            continue
        rec["calendars"] = g.get("calendars")
        rec["calendar_meta"] = g.get("calendar_meta")
        # Use existing extracted to drive new top3/unresolved
        ex = rec.get("expected_extracted") or []
        rec["expected_top3"] = gdl._draft_top3(rec, ex)
        rec["expected_unresolved"] = gdl._draft_unresolved(rec)
    DRAFT_PATH.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in draft) + "\n"
    )
    # Rebuild review.md scoped to SCOPE
    review = gdl._render_review(draft, set(SCOPE))
    (DRAFT_PATH.parent / "golden_30.draft.review.md").write_text(review)


def main() -> int:
    print("=" * 70)
    print("C4b (γ) — auto-tune select_guaranteed weights against p01~p05")
    print("=" * 70)

    final_level: int | None = None
    final_lines: list[str] = []
    final_counts: tuple[int, int] | None = None
    final_weights: dict[str, int] | None = None

    for level_idx, weights in enumerate(WEIGHTS_LADDER, start=1):
        label = f"{level_idx}차"
        print(f"\n--- {label} weights: {weights} ---")
        # 1. Regenerate calendars
        _summary, records = gac.run(weights=weights)
        # 2. Rebuild p01-p05 drafts (reuses existing expected_extracted)
        _rebuild_drafts_for_scope(records)
        # 3. Measure alignment
        matches, unambiguous, lines = _measure_alignment(records)
        for l in lines:
            print(l)
        print(
            f"  → {label}: matches={matches}/{len(SCOPE)} "
            f"(unambiguous={unambiguous})"
        )
        if matches >= 4:
            final_level = level_idx
            final_lines = lines
            final_counts = (matches, unambiguous)
            final_weights = weights
            break

    print()
    print("=" * 70)
    if final_level is None:
        print("⚠ All 3 weight levels failed to reach 4/5 — escalate to "
              "scenario-synthesis review (per the C4b brief).")
        return 1
    print(f"✅ Converged at {final_level}차 weights")
    print(f"   weights: {final_weights}")
    m, u = final_counts  # type: ignore[misc]
    print(f"   alignment: {m}/{len(SCOPE)} (unambiguous {u})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
