"""Step 5: end-to-end integration test.

Two passes over the same golden scenario:
    A. Text input: feed conversation directly into the pipeline.
    B. Image input: render the same conversation as a synthetic KakaoTalk
       screenshot, run it through OCR + reformat + the full pipeline.

Both passes must converge on the same set of grounded (participant,
time_expr_raw) pairs. Date/time intervals are compared loosely (per
participant set, not exact ISO equality) because the multi-day expansion
isn't fully deterministic across runs.

This isn't a single-shot CI gate -- it makes ~14 API calls per run (Step 4
OCR + reformat, then Steps 1-3 twice over). Use it as a smoke check after
non-trivial pipeline edits.
"""

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

from pipeline import run
from step4_ocr_ingest import render_kakao_screenshot
from upstage_client import UpstageClient


REFERENCE_DATE = "2026-05-11"

# Golden: 3-person mixed prefer/exclude. Same content for both modes.
CONVERSATION_TEXT = textwrap.dedent(
    """\
    민지: 이번주 늦게 보자
    준호: 금요일 저녁 좋아
    지수: 토요일 낮은 안돼
    """
).strip()

BUBBLES = [
    ("민지", "이번주 늦게 보자", "오후 8:31"),
    ("준호", "금요일 저녁 좋아", "오후 8:33"),
    ("지수", "토요일 낮은 안돼", "오후 8:35"),
]

EXPECT_PARTICIPANTS = {"민지", "준호", "지수"}
# Tolerant matching: substring presence per participant, not exact phrase.
EXPECT_PHRASE_SUBSTR = {
    "민지": "늦게",
    "준호": "금요일 저녁",
    "지수": "토요일",
}
EXPECT_POLARITY = {
    "민지": "prefer",
    "준호": "prefer",
    "지수": "exclude",
}


def _summarize_grounded(grounded: list[dict]) -> dict[str, dict]:
    """Collapse multi-day-expanded rows to {participant: {phrase, polarity, row_count}}."""
    by_part: dict[str, dict] = {}
    for it in grounded:
        p = it["participant"]
        agg = by_part.setdefault(
            p,
            {
                "time_expr_raw": it["time_expr_raw"],
                "polarity": it["polarity"],
                "row_count": 0,
                "starts": [],
            },
        )
        agg["row_count"] += 1
        agg["starts"].append(it["start"])
    return by_part


def _check_pass(label: str, result: dict) -> bool:
    grounded = result["step3_result"]["grounded"]
    unresolved = result["step3_result"]["unresolved"]
    summary = _summarize_grounded(grounded)

    print(f"-- {label}: chat_text fed to Step 1 --")
    print(result["chat_text"])
    print(f"-- {label}: grounded summary --")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"-- {label}: unresolved count = {len(unresolved)} --")
    if unresolved:
        for u in unresolved:
            print(
                f"   * {u['participant']} '{u['time_expr_raw']}' "
                f"verdict={u['gc_verdict']} reason={u['gc_reason']}"
            )

    failures: list[str] = []
    speakers = set(summary)
    missing = EXPECT_PARTICIPANTS - speakers
    if missing:
        failures.append(f"missing speakers in grounded: {missing}")
    extra = speakers - EXPECT_PARTICIPANTS
    if extra:
        failures.append(f"unexpected speakers in grounded: {extra}")
    for p, want_sub in EXPECT_PHRASE_SUBSTR.items():
        got = summary.get(p, {}).get("time_expr_raw", "")
        if want_sub not in got:
            failures.append(
                f"{p}: expected phrase containing {want_sub!r}, got {got!r}"
            )
    for p, want_pol in EXPECT_POLARITY.items():
        got = summary.get(p, {}).get("polarity")
        if got != want_pol:
            failures.append(f"{p}: expected polarity {want_pol}, got {got}")
    if unresolved:
        failures.append(
            f"{len(unresolved)} unresolved rows (golden case should be 100% grounded)"
        )

    if failures:
        print(f"[{label} FAIL]")
        for f in failures:
            print(f"  - {f}")
        return False
    print(f"[{label} OK]")
    return True


def _convergence_check(text_result: dict, image_result: dict) -> bool:
    """Both modes must produce the same (participant, polarity) set
    and substring-matching phrases."""
    t_sum = _summarize_grounded(text_result["step3_result"]["grounded"])
    i_sum = _summarize_grounded(image_result["step3_result"]["grounded"])
    failures = []
    if set(t_sum) != set(i_sum):
        failures.append(
            f"participant sets differ: text={set(t_sum)}, image={set(i_sum)}"
        )
    for p in set(t_sum) & set(i_sum):
        if t_sum[p]["polarity"] != i_sum[p]["polarity"]:
            failures.append(
                f"{p}: polarity differs (text={t_sum[p]['polarity']}, "
                f"image={i_sum[p]['polarity']})"
            )
        # phrase substring overlap is sufficient (OCR may add a space, etc.)
        tp, ip = t_sum[p]["time_expr_raw"], i_sum[p]["time_expr_raw"]
        norm = lambda s: "".join(s.split())
        if norm(tp) not in norm(ip) and norm(ip) not in norm(tp):
            failures.append(
                f"{p}: phrases don't overlap (text={tp!r}, image={ip!r})"
            )
    if failures:
        print("[CONVERGENCE FAIL]")
        for f in failures:
            print(f"  - {f}")
        return False
    print("[CONVERGENCE OK] text and image modes agree on participants/polarity/phrases")
    return True


def main() -> int:
    client = UpstageClient()

    # Pass A: text input
    print("=" * 72)
    print("PASS A: text input")
    print("=" * 72)
    text_result = run(
        reference_date=REFERENCE_DATE,
        conversation_text=CONVERSATION_TEXT,
        client=client,
    )
    a_ok = _check_pass("text", text_result)

    # Pass B: image input
    print("=" * 72)
    print("PASS B: image input (synthetic KakaoTalk screenshot)")
    print("=" * 72)
    img_dir = Path(__file__).resolve().parent.parent / "assets" / "step5_tmp"
    img_path = img_dir / "golden_3person.png"
    render_kakao_screenshot(img_path, title="동기 모임", bubbles=BUBBLES)
    image_result = run(
        reference_date=REFERENCE_DATE,
        image_path=img_path,
        client=client,
    )
    b_ok = _check_pass("image", image_result)

    # Cross-mode convergence
    print("=" * 72)
    print("CONVERGENCE CHECK: text mode == image mode (modulo phrase whitespace)")
    print("=" * 72)
    c_ok = _convergence_check(text_result, image_result)

    print("=" * 72)
    passed = sum([a_ok, b_ok, c_ok])
    print(f"SUMMARY: {passed}/3 integration checks passed")
    return 0 if passed == 3 else 1


if __name__ == "__main__":
    sys.exit(main())
