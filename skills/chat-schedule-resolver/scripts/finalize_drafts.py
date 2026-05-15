"""C4b finalisation — classify all 30 drafts and render the final review.md.

Categories (per the C4b (I) brief):
    clear_consensus_match   ✅  consensus is unambiguous AND Top-1 weekday == consensus
    ambiguous_consensus     △   consensus undefined (no clear winner in extracted prefer rows)
    trap_filter_active      △   consensus weekday is in blocked_weekdays
                                (filter intentionally blocked the consensus — desired behavior)
    misalignment            ❌  none of the above — real algorithmic mismatch

Misalignment guard: if W ≥ 3 (10% of 30), stop and raise — algorithm review needed.

Outputs:
  * Final review.md with category summary at the top:
        assets/golden/golden_30.review.md
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

import generate_draft_labels as gdl
from data.select_guaranteed import consensus_weekday
from tune_weights_and_redraft import WEEKDAY_KR


SKILL_ROOT = Path(__file__).resolve().parent.parent
DRAFT_PATH = SKILL_ROOT / "assets" / "golden" / "golden_30.draft.jsonl"
FINAL_REVIEW_PATH = SKILL_ROOT / "assets" / "golden" / "golden_30.review.md"

MISALIGN_GUARD = 3   # W ≥ 3 → stop and report


def classify(rec: dict) -> tuple[str, dict]:
    """Return (category, diagnostic).

    diagnostic carries the data needed by the per-scenario row in review.md:
        consensus_label  — '금요일' / 'ambiguous'
        top1_label       — '월요일' / '없음'
        blocked          — list of blocked weekday/주말 labels
    """
    consensus_wd = consensus_weekday(rec["scenario"]["conversation"])
    meta = rec.get("calendar_meta") or {}
    gtd = meta.get("guaranteed_slots") or []
    blocked = list(meta.get("blocked_weekdays", []))
    if meta.get("block_weekend"):
        blocked.append("주말")
    top1_label = "없음"
    top1_wd: int | None = None
    if gtd:
        top1_wd = datetime.fromisoformat(gtd[0]["start"]).weekday()
        top1_label = WEEKDAY_KR[top1_wd] + "요일"
    consensus_label = (
        WEEKDAY_KR[consensus_wd] + "요일" if consensus_wd is not None else "ambiguous"
    )
    diag = {
        "consensus_label": consensus_label,
        "top1_label": top1_label,
        "blocked": blocked,
    }
    if consensus_wd is None:
        return "ambiguous_consensus", diag
    if top1_wd is not None and consensus_wd == top1_wd:
        return "clear_consensus_match", diag
    if WEEKDAY_KR[consensus_wd] in (meta.get("blocked_weekdays") or []):
        return "trap_filter_active", diag
    if consensus_wd in (5, 6) and meta.get("block_weekend"):
        return "trap_filter_active", diag
    return "misalignment", diag


WD_KR_FULL = ["월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일"]


def _slot_line(slot: dict) -> str:
    s = datetime.fromisoformat(slot["start"])
    e = datetime.fromisoformat(slot["end"])
    wd = WD_KR_FULL[s.weekday()]
    return f"{s.strftime('%m/%d')}({wd[0]}) {s.strftime('%H:%M')}~{e.strftime('%H:%M')}"


def render_final_review(records: list[dict], categories: dict[str, list[tuple[str, dict]]]) -> str:
    lines: list[str] = []
    total = len(records)
    n_clear = len(categories["clear_consensus_match"])
    n_ambig = len(categories["ambiguous_consensus"])
    n_trap = len(categories["trap_filter_active"])
    n_misal = len(categories["misalignment"])

    lines.append(f"# golden_30 — final review (reference_date = 2026-05-11)")
    lines.append("")
    lines.append("## 30건 라벨링 결과 요약")
    lines.append("")
    lines.append(f"- ✅ **Clear consensus match**: **{n_clear}/{total}**  대화 합의가 명확하고 Top-1과 일치")
    lines.append(f"- △ **Ambiguous consensus**: **{n_ambig}/{total}**  대화 자체에 합의가 없음 (측정 메소드론상 △)")
    lines.append(f"- △ **Trap filter active**: **{n_trap}/{total}**  consensus 요일이 filter에 의해 차단됨 (의도된 동작)")
    lines.append(f"- ❌ **Misalignment**: **{n_misal}/{total}**  검토 필요 (W ≥ {MISALIGN_GUARD}이면 알고리즘 점검 권장)")
    lines.append("")
    if n_misal >= MISALIGN_GUARD:
        lines.append(f"> ⚠ Misalignment {n_misal}건이 임계 {MISALIGN_GUARD}건 이상 — 알고리즘 검토 필요.")
        lines.append("")
    lines.append("### Misalignment 시나리오 (라벨러 우선 확인)")
    if n_misal == 0:
        lines.append("_(없음 — 30건 모두 ✅ / △ 카테고리)_")
    else:
        for sid, diag in categories["misalignment"]:
            lines.append(
                f"- **{sid}**: consensus = `{diag['consensus_label']}`, "
                f"Top-1 = `{diag['top1_label']}`, blocked = `{diag['blocked'] or '없음'}`"
            )
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("각 시나리오의 자동 초안을 훑으며 명백히 틀린 부분만 메모해주세요.")
    lines.append("Misalignment 카테고리 위주로 보고, 나머지는 spot-check.")
    lines.append("")

    # Per-scenario body
    cat_of: dict[str, str] = {}
    for cat_name, items in categories.items():
        for sid, _ in items:
            cat_of[sid] = cat_name

    for rec in records:
        sid = rec["scenario_id"]
        spec = rec.get("spec") or {}
        meta = rec.get("calendar_meta", {}) or {}
        density = meta.get("density", "?")
        trap = meta.get("is_trap_effective", False)
        cat = cat_of.get(sid, "?")
        cat_mark = {
            "clear_consensus_match": "✅",
            "ambiguous_consensus": "△ ambig",
            "trap_filter_active": "△ trap",
            "misalignment": "❌ misalign",
        }.get(cat, "?")

        lines.append(f"## {sid}  ({spec.get('topic','?')}, n={spec.get('people_n','?')}, "
                     f"density={density}, trap={'true' if trap else 'false'}) — {cat_mark}")
        lines.append("")
        lines.append("**Conversation**")
        lines.append("")
        for i, m in enumerate(rec["scenario"]["conversation"], 1):
            lines.append(f"[{i}] {m['user']} ({m.get('ts','')}): {m['text']}")
        lines.append("")

        ex = rec.get("expected_extracted") or []
        lines.append(f"**Draft expected_extracted ({len(ex)}건)**")
        lines.append("")
        for i, e in enumerate(ex, 1):
            lines.append(
                f"{i}. {e['who']} / {e['type']} / \"{e['time_expr_raw']}\" / msg_{e['evidence_msg_id']}"
            )
        if not ex:
            lines.append("_(없음)_")
        lines.append("")

        top3 = rec.get("expected_top3") or []
        lines.append(f"**Draft expected_top3 ({len(top3)}건)**")
        lines.append("")
        for i, s in enumerate(top3, 1):
            sc = s.get("score")
            head_score = f"  [score={sc}]" if sc is not None else ""
            lines.append(f"{i}. {_slot_line(s)}{head_score}")
            # rationale already contains breakdown lines as newline-separated
            for ln in (s.get("rationale") or "").split("\n")[1:]:
                lines.append(f"   {ln}")
        if not top3:
            lines.append("_(없음 — 캘린더 교집합 부족)_")
        lines.append("")

        unr = rec.get("expected_unresolved") or []
        lines.append(f"**Draft expected_unresolved ({len(unr)}건)**")
        lines.append("")
        for u in unr:
            lines.append(f"- {u.get('who','*')} / `{u.get('time_expr_raw','')}` — {u.get('reason','')}")
        if not unr:
            lines.append("_(없음)_")
        lines.append("")
        lines.append("---")
        lines.append("**검토**")
        lines.append("- [ ] 모두 정확")
        lines.append("- [ ] 수정 필요: <메모>")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    records = [json.loads(l) for l in DRAFT_PATH.read_text().splitlines()]
    if len(records) != 30:
        print(f"[WARN] expected 30 records, got {len(records)}")

    categories: dict[str, list[tuple[str, dict]]] = {
        "clear_consensus_match": [],
        "ambiguous_consensus": [],
        "trap_filter_active": [],
        "misalignment": [],
    }
    for rec in records:
        cat, diag = classify(rec)
        categories[cat].append((rec["scenario_id"], diag))

    # Print summary
    print(f"[Classify] clear={len(categories['clear_consensus_match'])} "
          f"ambig={len(categories['ambiguous_consensus'])} "
          f"trap={len(categories['trap_filter_active'])} "
          f"misalign={len(categories['misalignment'])}")
    if categories["misalignment"]:
        print("[Misalignment scenarios]")
        for sid, diag in categories["misalignment"]:
            print(f"   {sid}: consensus={diag['consensus_label']} "
                  f"top1={diag['top1_label']} blocked={diag['blocked']}")

    review = render_final_review(records, categories)
    FINAL_REVIEW_PATH.write_text(review)
    print(f"[Review.md] {FINAL_REVIEW_PATH}")

    n_misal = len(categories["misalignment"])
    if n_misal >= MISALIGN_GUARD:
        print(f"⚠ misalignment {n_misal} ≥ {MISALIGN_GUARD} — stop and report")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
