"""Step 3: groundedness gate.

Takes Step 2's normalized items and partitions them into:
    - grounded:    backed by a specific utterance in the original chat.
    - unresolved:  hallucinated or unverifiable (drop from candidate ranking).

The dedicated Upstage Groundedness Check model is deprecated (see
``UpstageClient.check_groundedness`` for the discovery trail), so this step
uses Solar Pro 3 as an LLM-judge with strict JSON output.

Optimization: we judge per *unique* (participant, time_expr_raw) pair rather
than per-row, since Step 2 emits many rows per phrase (multi-day expansion)
and the judge verdict only depends on the source phrase.

Pipeline contract:
    in : (conversation_text, step2_result)
    out: {
        "grounded":   [step2_item, ...]   # passed gate
        "unresolved": [step2_item, ...]   # failed gate, with .gc_reason attached
        "verdicts":   [{participant, time_expr_raw, verdict, score,
                        evidence_msg_id, reason}, ...]
    }
"""

from __future__ import annotations

import json
import sys
import textwrap
from typing import Any

from step2_normalize import (
    CASES as STEP2_CASES,
    normalize as step2_normalize,
)
from upstage_client import UpstageClient


def _number_lines(text: str) -> str:
    return "\n".join(
        f"{i + 1}: {line}"
        for i, line in enumerate(text.splitlines())
        if line.strip()
    )


def filter_grounded(
    client: UpstageClient,
    *,
    conversation_text: str,
    step2_result: dict[str, Any],
    pass_thresholds: tuple[str, ...] = ("grounded",),
) -> dict[str, Any]:
    """Return {grounded, unresolved, verdicts}.

    A row passes when its (participant, time_expr_raw) verdict is in
    ``pass_thresholds``. Defaults to ``("grounded",)`` so ``unsure`` rows
    are dropped to ``unresolved`` (conservative).
    """
    numbered_ctx = _number_lines(conversation_text)

    seen: dict[tuple[str, str], dict[str, Any]] = {}
    for it in step2_result.get("items", []):
        key = (it["participant"], it["time_expr_raw"])
        if key in seen:
            continue
        claim = (
            f"{it['participant']}는(은) '{it['time_expr_raw']}'을(를) "
            f"{'선호한다' if it['polarity'] == 'prefer' else '배제한다'}."
        )
        verdict = client.check_groundedness(context=numbered_ctx, claim=claim)
        verdict_record = {
            "participant": it["participant"],
            "time_expr_raw": it["time_expr_raw"],
            "polarity": it["polarity"],
            "claim": claim,
            **verdict,
        }
        seen[key] = verdict_record

    grounded: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    for it in step2_result.get("items", []):
        v = seen[(it["participant"], it["time_expr_raw"])]
        annotated = dict(it)
        annotated["gc_verdict"] = v["verdict"]
        annotated["gc_score"] = v["score"]
        annotated["gc_evidence_msg_id"] = v["evidence_msg_id"]
        annotated["gc_reason"] = v["reason"]
        if v["verdict"] in pass_thresholds:
            grounded.append(annotated)
        else:
            unresolved.append(annotated)

    return {
        "grounded": grounded,
        "unresolved": unresolved,
        "verdicts": list(seen.values()),
    }


# ---- self-test -------------------------------------------------------------

# Case A: 깨끗한 입력 — 전부 grounded여야 함.
# Case B: hallucination 주입 — Step 2 결과에 chat에 없는 participant/phrase를
#         수동으로 추가하고, 그 항목들이 unresolved로 빠지는지 확인.
# Case C: ambiguous phrase that is in the chat — should still be grounded.

CASE_CLEAN = {
    "name": "Clean 3-person (everything grounded)",
    "reference_date": "2026-05-11",
    "conversation": textwrap.dedent(
        """\
        민지: 이번주 늦게 보자
        준호: 금요일 저녁 좋아
        지수: 토요일 낮은 안돼
        """
    ),
}

CASE_AMBIG = {
    "name": "Ambiguous-but-grounded (weekend exclude)",
    "reference_date": "2026-05-11",
    "conversation": textwrap.dedent(
        """\
        태윤: 주말은 절대 안돼
        """
    ),
}

# For the hallucination case, we run Step 2 on a clean chat and then inject
# fake items. The injected ones MUST land in 'unresolved'.
INJECTED_HALLUCINATIONS = [
    {
        "participant": "민지",
        "polarity": "prefer",
        "time_expr_raw": "화요일 오전",  # 민지 never said this
        "start": "2026-05-12T09:00",
        "end": "2026-05-12T12:00",
        "certainty": 0.5,
        "source_msg_id": 1,
    },
    {
        "participant": "유나",  # 유나 isn't in the chat at all
        "polarity": "exclude",
        "time_expr_raw": "수요일 저녁",
        "start": "2026-05-13T18:00",
        "end": "2026-05-13T21:00",
        "certainty": 0.4,
        "source_msg_id": 1,
    },
]


def _print_verdict_table(verdicts: list[dict[str, Any]]) -> None:
    print(f"{'participant':<8} {'time_expr_raw':<22} {'verdict':<14} score  msg  reason")
    print("-" * 88)
    for v in verdicts:
        print(
            f"{v['participant']:<8} {v['time_expr_raw'][:22]:<22} "
            f"{v['verdict']:<14} {v['score']:<5.2f} "
            f"{str(v.get('evidence_msg_id')):<4} {v['reason']}"
        )


def main() -> int:
    client = UpstageClient()
    cases = []

    # Case A: clean
    print("=" * 72)
    print(f"CASE A: {CASE_CLEAN['name']}")
    s1 = client.infer_time_preferences(
        CASE_CLEAN["conversation"], reference_date=CASE_CLEAN["reference_date"]
    )
    s2, _ = step2_normalize(
        client,
        conversation_text=CASE_CLEAN["conversation"],
        step1_output=s1,
        reference_date=CASE_CLEAN["reference_date"],
    )
    out = filter_grounded(
        client, conversation_text=CASE_CLEAN["conversation"], step2_result=s2
    )
    _print_verdict_table(out["verdicts"])
    n_uniq = len(out["verdicts"])
    n_grounded_uniq = sum(1 for v in out["verdicts"] if v["verdict"] == "grounded")
    case_a_pass = (
        n_uniq >= 3
        and n_grounded_uniq == n_uniq
        and len(out["unresolved"]) == 0
    )
    print(
        f"unique phrases: {n_uniq}, grounded: {n_grounded_uniq}, "
        f"unresolved rows: {len(out['unresolved'])}"
    )
    print("[OK]" if case_a_pass else "[FAIL] expected ALL grounded, no unresolved")
    cases.append(case_a_pass)

    # Case B: hallucination injection
    print("=" * 72)
    print("CASE B: Hallucination injection on top of Case A's Step 2 result")
    s2_b = dict(s2)
    s2_b["items"] = list(s2["items"]) + INJECTED_HALLUCINATIONS
    s2_b["participants"] = list(set(s2["participants"]) | {"유나"})
    out_b = filter_grounded(
        client, conversation_text=CASE_CLEAN["conversation"], step2_result=s2_b
    )
    _print_verdict_table(out_b["verdicts"])
    # Injected items MUST be in unresolved.
    injected_keys = {(h["participant"], h["time_expr_raw"]) for h in INJECTED_HALLUCINATIONS}
    grounded_keys = {(it["participant"], it["time_expr_raw"]) for it in out_b["grounded"]}
    leaked = injected_keys & grounded_keys
    real_keys = {
        (it["participant"], it["time_expr_raw"])
        for it in s2["items"]
    }
    dropped_real = real_keys - grounded_keys
    case_b_pass = not leaked and not dropped_real
    print(f"leaked hallucinations (should be empty): {leaked}")
    print(f"real items dropped (should be empty):    {dropped_real}")
    print("[OK]" if case_b_pass else "[FAIL]")
    cases.append(case_b_pass)

    # Case C: ambiguous-but-grounded
    print("=" * 72)
    print(f"CASE C: {CASE_AMBIG['name']}")
    s1c = client.infer_time_preferences(
        CASE_AMBIG["conversation"], reference_date=CASE_AMBIG["reference_date"]
    )
    s2c, _ = step2_normalize(
        client,
        conversation_text=CASE_AMBIG["conversation"],
        step1_output=s1c,
        reference_date=CASE_AMBIG["reference_date"],
    )
    out_c = filter_grounded(
        client, conversation_text=CASE_AMBIG["conversation"], step2_result=s2c
    )
    _print_verdict_table(out_c["verdicts"])
    case_c_pass = (
        len(out_c["verdicts"]) >= 1
        and all(v["verdict"] == "grounded" for v in out_c["verdicts"])
    )
    print("[OK]" if case_c_pass else "[FAIL] expected grounded for 태윤's 주말")
    cases.append(case_c_pass)

    print("=" * 72)
    passed = sum(cases)
    print(f"SUMMARY: {passed}/{len(cases)} cases passed")
    return 0 if passed == len(cases) else 1


if __name__ == "__main__":
    sys.exit(main())
