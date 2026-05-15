"""Step 3: evidence verification gate.

Takes Step 2's normalized items and partitions them into:
    - grounded:    backed by a specific utterance in the original chat.
    - unresolved:  hallucinated or unverifiable (drop from candidate ranking).

Upstage's dedicated evidence-verification model has been removed from the
public API surface; we implement the equivalent gate as Solar Pro 3 acting
as an LLM-judge with strict JSON output (see
``UpstageClient.verify_evidence``).

Optimization: we judge per *unique* (who, time) pair rather than per-row,
since Step 2 emits many rows per phrase (multi-day expansion) and the judge
verdict only depends on the source phrase.

Pipeline contract:
    in : (conversation_text, step2_result)
    out: {
        "grounded":   [step2_item + grounded=True, ...]  # passed gate
        "unresolved": [step2_item + grounded=False, ...] # failed gate
        "verdicts":   [{who, time, type, claim,
                        evidence_verified, supporting_msg_ids,
                        reason, judge_response}, ...]
    }

Annotated rows in `grounded`/`unresolved` carry:
  * `grounded`: bool (SKILL.md public contract field; True iff the row
                     passed the gate; identical to evidence_verified).
  * `supporting_msg_ids`: list[int] — 1-based lines that back the claim.
  * `reason`: str — one short Korean sentence from the judge.
  * `judge_response`: dict — full parsed judge JSON for the (who, time)
                     pair this row came from (kept for debugging /
                     auditability; opaque to the rest of the pipeline).

The downstream skill output (`extracted_preferences` in SKILL.md) surfaces
only the 5-field contract — `{who, type, time, evidence_msg_id, grounded}`.
For that contract's ``evidence_msg_id`` (singular int), use the first
element of ``supporting_msg_ids`` (or the original Step 2
``evidence_msg_id`` for rows that fail the gate).
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


def verify_extracted_preferences(
    client: UpstageClient,
    *,
    conversation_text: str,
    step2_result: dict[str, Any],
) -> dict[str, Any]:
    """Run the Evidence Verification gate on Step 2's items.

    Returns ``{grounded, unresolved, verdicts}``. A row passes iff the
    judge for its ``(who, time)`` pair returned
    ``evidence_verified=true``.
    """
    numbered_ctx = _number_lines(conversation_text)

    seen: dict[tuple[str, str], dict[str, Any]] = {}
    for it in step2_result.get("items", []):
        key = (it["who"], it["time"])
        if key in seen:
            continue
        claim = (
            f"{it['who']}는(은) '{it['time']}'을(를) "
            f"{'선호한다' if it['type'] == 'prefer' else '배제한다'}."
        )
        judge_response = client.verify_evidence(context=numbered_ctx, claim=claim)
        seen[key] = {
            "who": it["who"],
            "time": it["time"],
            "type": it["type"],
            "claim": claim,
            "judge_response": judge_response,
            "evidence_verified": judge_response["evidence_verified"],
            "supporting_msg_ids": judge_response["supporting_msg_ids"],
            "reason": judge_response["reason"],
        }

    grounded: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    for it in step2_result.get("items", []):
        v = seen[(it["who"], it["time"])]
        annotated = dict(it)
        annotated["grounded"] = v["evidence_verified"]
        annotated["supporting_msg_ids"] = v["supporting_msg_ids"]
        annotated["reason"] = v["reason"]
        annotated["judge_response"] = v["judge_response"]
        if v["evidence_verified"]:
            grounded.append(annotated)
        else:
            unresolved.append(annotated)

    return {
        "grounded": grounded,
        "unresolved": unresolved,
        "verdicts": list(seen.values()),
    }


# ---- self-test -------------------------------------------------------------

# Case A: 깨끗한 입력 — 전부 verified여야 함.
# Case B: hallucination 주입 — Step 2 결과에 chat에 없는 who/time을 수동으로
#         추가하고, 그 항목들이 unresolved로 빠지는지 확인 (회귀 테스트).
# Case C: ambiguous phrase that is in the chat — should still be verified.

CASE_CLEAN = {
    "name": "Clean 3-person (everything verified)",
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
    "name": "Ambiguous-but-verified (weekend exclude)",
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
        "who": "민지",
        "type": "prefer",
        "time": "화요일 오전",  # 민지 never said this
        "start": "2026-05-12T09:00",
        "end": "2026-05-12T12:00",
        "certainty": 0.5,
        "evidence_msg_id": 1,
    },
    {
        "who": "유나",  # 유나 isn't in the chat at all
        "type": "exclude",
        "time": "수요일 저녁",
        "start": "2026-05-13T18:00",
        "end": "2026-05-13T21:00",
        "certainty": 0.4,
        "evidence_msg_id": 1,
    },
    {
        # Regression: 지수 IS in the chat but said 토요일 낮, not 월요일.
        # The judge must reject this even though the speaker name matches.
        "who": "지수",
        "type": "exclude",
        "time": "월요일",
        "start": "2026-05-11T00:00",
        "end": "2026-05-12T00:00",
        "certainty": 0.5,
        "evidence_msg_id": 3,
    },
]


def _print_verdict_table(verdicts: list[dict[str, Any]]) -> None:
    print(f"{'who':<8} {'time':<22} {'verified':<10} msg_ids  reason")
    print("-" * 88)
    for v in verdicts:
        print(
            f"{v['who']:<8} {v['time'][:22]:<22} "
            f"{str(v['evidence_verified']):<10} "
            f"{str(v['supporting_msg_ids']):<8} {v['reason']}"
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
    out = verify_extracted_preferences(
        client, conversation_text=CASE_CLEAN["conversation"], step2_result=s2
    )
    _print_verdict_table(out["verdicts"])
    n_uniq = len(out["verdicts"])
    n_verified = sum(1 for v in out["verdicts"] if v["evidence_verified"])
    case_a_pass = (
        n_uniq >= 3
        and n_verified == n_uniq
        and len(out["unresolved"]) == 0
    )
    print(
        f"unique phrases: {n_uniq}, verified: {n_verified}, "
        f"unresolved rows: {len(out['unresolved'])}"
    )
    print("[OK]" if case_a_pass else "[FAIL] expected ALL verified, no unresolved")
    cases.append(case_a_pass)

    # Case B: hallucination injection (regression test)
    print("=" * 72)
    print("CASE B: Hallucination injection on top of Case A's Step 2 result")
    s2_b = dict(s2)
    s2_b["items"] = list(s2["items"]) + INJECTED_HALLUCINATIONS
    s2_b["participants"] = list(set(s2["participants"]) | {"유나"})
    out_b = verify_extracted_preferences(
        client, conversation_text=CASE_CLEAN["conversation"], step2_result=s2_b
    )
    _print_verdict_table(out_b["verdicts"])
    # Injected items MUST be in unresolved.
    injected_keys = {(h["who"], h["time"]) for h in INJECTED_HALLUCINATIONS}
    grounded_keys = {(it["who"], it["time"]) for it in out_b["grounded"]}
    leaked = injected_keys & grounded_keys
    real_keys = {
        (it["who"], it["time"])
        for it in s2["items"]
    }
    dropped_real = real_keys - grounded_keys
    case_b_pass = not leaked and not dropped_real
    print(f"leaked hallucinations (should be empty): {leaked}")
    print(f"real items dropped (should be empty):    {dropped_real}")
    print("[OK]" if case_b_pass else "[FAIL]")
    cases.append(case_b_pass)

    # Case C: ambiguous-but-verified
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
    out_c = verify_extracted_preferences(
        client, conversation_text=CASE_AMBIG["conversation"], step2_result=s2c
    )
    _print_verdict_table(out_c["verdicts"])
    case_c_pass = (
        len(out_c["verdicts"]) >= 1
        and all(v["evidence_verified"] for v in out_c["verdicts"])
    )
    print("[OK]" if case_c_pass else "[FAIL] expected verified for 태윤's 주말")
    cases.append(case_c_pass)

    print("=" * 72)
    passed = sum(cases)
    print(f"SUMMARY: {passed}/{len(cases)} cases passed")
    return 0 if passed == len(cases) else 1


if __name__ == "__main__":
    sys.exit(main())
