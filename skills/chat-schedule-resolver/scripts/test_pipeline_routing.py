"""Unit tests for pipeline.prepare_chat_text — 4 input-routing combos.

No real API calls. We stub ``ingest_kakao_image`` so each test stays
fast and offline; the routing logic (which combination of inputs goes
where, and which OCR'd messages get surfaced as source_notes) is pure
Python and worth testing in isolation.

Run: python3 scripts/test_pipeline_routing.py
Exit code 0 if all 4 cases pass, non-zero otherwise.
"""

from __future__ import annotations

import sys
from typing import Any
from unittest.mock import patch

import pipeline


# ---- stubs -----------------------------------------------------------------

class _StubClient:
    """Stand-in for UpstageClient. ingest_kakao_image is patched, so the
    client itself is never actually called in these tests — we just need
    *something* truthy that satisfies ``client = client or UpstageClient()``
    without triggering UPSTAGE_API_KEY loading."""

    pass


def _fake_ingest_kakao_image(messages_by_path: dict[str, list[dict[str, str]]]):
    """Return a stub ``ingest_kakao_image`` that yields canned OCR results
    keyed by image path. Each value is a list of ``{user, text, ts}`` dicts."""

    def _impl(client: Any, image_path: Any) -> dict[str, Any]:
        path = str(image_path)
        messages = messages_by_path.get(path, [])
        chat_lines = "\n".join(f"{m['user']}: {m['text']}" for m in messages)
        return {
            "chat_lines": chat_lines,
            "messages": messages,
            "ocr_raw": chat_lines,  # not realistic; routing tests don't care
        }

    return _impl


# ---- cases -----------------------------------------------------------------

def case_text_only() -> bool:
    """conversation_text 단독 → 그대로 chat_text. OCR/source_notes 없음."""
    conv = "민지: 이번주 늦게 보자\n준호: 금요일 저녁 좋아"
    out = pipeline.prepare_chat_text(
        client=_StubClient(),
        conversation_text=conv,
        image_paths=None,
    )
    failures = []
    if out["chat_text"] != conv.strip():
        failures.append(f"chat_text != conversation: {out['chat_text']!r}")
    if out["step4_results"] != []:
        failures.append(f"step4_results should be empty: {out['step4_results']}")
    if out["source_notes"] != []:
        failures.append(f"source_notes should be empty: {out['source_notes']}")
    return _report("text_only", failures)


def case_img_only() -> bool:
    """image_paths 단독 → OCR 결과가 chat_text가 되고 source_notes 없음."""
    canned = {
        "img1.png": [
            {"user": "민지", "text": "이번주 늦게 보자", "ts": ""},
            {"user": "준호", "text": "금요일 저녁 좋아", "ts": ""},
        ]
    }
    with patch.object(pipeline, "ingest_kakao_image", _fake_ingest_kakao_image(canned)):
        out = pipeline.prepare_chat_text(
            client=_StubClient(),
            conversation_text=None,
            image_paths=["img1.png"],
        )
    failures = []
    expected_chat = "민지: 이번주 늦게 보자\n준호: 금요일 저녁 좋아"
    if out["chat_text"] != expected_chat:
        failures.append(f"chat_text != OCR: {out['chat_text']!r}")
    if len(out["step4_results"]) != 1:
        failures.append(f"step4_results len != 1: {out['step4_results']}")
    if out["source_notes"] != []:
        failures.append(
            f"source_notes should be empty (no conversation to diff against): "
            f"{out['source_notes']}"
        )
    return _report("img_only", failures)


def case_both_with_ocr_only_msg() -> bool:
    """둘 다: conversation이 권위. OCR에만 있는 메시지는 source_notes에 surface."""
    conv = "민지: 이번주 늦게 보자\n준호: 금요일 저녁 좋아"
    canned = {
        "img1.png": [
            # First two match conversation (whitespace-flex tolerated)
            {"user": "민지", "text": "이번주 늦게 보자", "ts": ""},
            {"user": "준호", "text": "금요일 저녁 좋아 ", "ts": ""},  # trailing space
            # This one is OCR-only — must land in source_notes
            {"user": "지수", "text": "토요일 낮은 안돼", "ts": ""},
        ]
    }
    with patch.object(pipeline, "ingest_kakao_image", _fake_ingest_kakao_image(canned)):
        out = pipeline.prepare_chat_text(
            client=_StubClient(),
            conversation_text=conv,
            image_paths=["img1.png"],
        )
    failures = []
    # chat_text must come from conversation, not be merged with OCR
    if out["chat_text"] != conv.strip():
        failures.append(
            "chat_text should equal conversation (authoritative), not merge "
            f"with OCR: {out['chat_text']!r}"
        )
    # Exactly one OCR-only message should be surfaced (지수)
    ocr_only = [n for n in out["source_notes"] if n["source"] == "ocr_only"]
    if len(ocr_only) != 1:
        failures.append(
            f"expected exactly 1 ocr_only note (지수); got {len(ocr_only)}: "
            f"{ocr_only}"
        )
    elif ocr_only[0]["user"] != "지수" or "토요일" not in ocr_only[0]["text"]:
        failures.append(f"wrong ocr_only note content: {ocr_only[0]}")
    # 민지/준호 must NOT be in notes (they match conversation modulo whitespace)
    for n in ocr_only:
        if n["user"] in ("민지", "준호"):
            failures.append(
                f"{n['user']} matched conversation but was surfaced: {n}"
            )
    return _report("both_with_ocr_only_msg", failures)


def case_neither() -> bool:
    """둘 다 없음 → ValueError. API 호출 전 즉시 실패."""
    failures = []
    try:
        pipeline.prepare_chat_text(
            client=_StubClient(),
            conversation_text=None,
            image_paths=None,
        )
        failures.append("expected ValueError, got success")
    except ValueError as e:
        if "conversation_text" not in str(e) and "image_paths" not in str(e):
            failures.append(f"ValueError msg should mention both inputs: {e}")
    # Also empty string should count as "no conversation"
    try:
        pipeline.prepare_chat_text(
            client=_StubClient(),
            conversation_text="   ",
            image_paths=[],
        )
        failures.append("empty conversation + empty image_paths should fail")
    except ValueError:
        pass
    return _report("neither", failures)


def case_run_rejects_bare_path() -> bool:
    """run() must reject a bare str/Path (single-image callers wrap as [p])."""
    failures = []
    try:
        pipeline.run(
            reference_date="2026-05-11",
            image_paths="img1.png",  # type: ignore[arg-type]
            client=_StubClient(),
        )
        failures.append("expected TypeError on bare str image_paths")
    except TypeError:
        pass
    return _report("run_rejects_bare_path", failures)


def case_verify_drops_unverified_rows() -> bool:
    """Evidence Verification regression: when the judge returns
    evidence_verified=false for a (who, time) pair, every row from that
    pair lands in `unresolved` and the row's `grounded` field is False.

    No real API call — we inject a fake ``verify_evidence`` on the stub
    client so the test is pure-Python.
    """
    import step3_verify

    # Hand-built Step 2 result: one valid row + one hallucinated row.
    step2 = {
        "participants": ["민지", "유나"],
        "items": [
            {
                "who": "민지", "type": "prefer", "time": "이번주 늦게",
                "start": "2026-05-15T20:00", "end": "2026-05-16T00:00",
                "certainty": 0.8, "evidence_msg_id": 1,
            },
            {
                # 유나 isn't in the conversation. Judge must reject.
                "who": "유나", "type": "exclude", "time": "수요일 저녁",
                "start": "2026-05-13T18:00", "end": "2026-05-13T21:00",
                "certainty": 0.4, "evidence_msg_id": 1,
            },
        ],
    }
    conv = "민지: 이번주 늦게 보자"

    canned_verdicts = {
        ("민지", "이번주 늦게"): {
            "evidence_verified": True,
            "supporting_msg_ids": [1],
            "reason": "민지가 1번 줄에서 직접 말함.",
        },
        ("유나", "수요일 저녁"): {
            "evidence_verified": False,
            "supporting_msg_ids": [],
            "reason": "유나는 context에 등장하지 않음.",
        },
    }

    class _JudgeStub:
        def verify_evidence(self, *, context: str, claim: str, **_) -> dict:
            for (who, time), v in canned_verdicts.items():
                if who in claim and time in claim:
                    return v
            raise AssertionError(f"unexpected claim in judge: {claim!r}")

    out = step3_verify.verify_extracted_preferences(
        _JudgeStub(),  # type: ignore[arg-type]
        conversation_text=conv,
        step2_result=step2,
    )

    failures = []
    if len(out["grounded"]) != 1:
        failures.append(f"expected 1 grounded row, got {len(out['grounded'])}")
    elif out["grounded"][0]["who"] != "민지":
        failures.append(
            f"민지 row should be grounded: got {out['grounded'][0]['who']!r}"
        )
    elif out["grounded"][0]["grounded"] is not True:
        failures.append("민지 row's `grounded` field must be True")
    if len(out["unresolved"]) != 1:
        failures.append(
            f"expected 1 unresolved row, got {len(out['unresolved'])}"
        )
    elif out["unresolved"][0]["who"] != "유나":
        failures.append(
            f"유나 row should be unresolved: got {out['unresolved'][0]['who']!r}"
        )
    elif out["unresolved"][0]["grounded"] is not False:
        failures.append("유나 row's `grounded` field must be False")
    # supporting_msg_ids and reason must surface on every row
    for r in out["grounded"] + out["unresolved"]:
        if "supporting_msg_ids" not in r or not isinstance(
            r["supporting_msg_ids"], list
        ):
            failures.append(f"row missing supporting_msg_ids: {r}")
        if "reason" not in r or not isinstance(r["reason"], str):
            failures.append(f"row missing reason: {r}")
    return _report("verify_drops_unverified_rows", failures)


# ---- runner ----------------------------------------------------------------

def _report(name: str, failures: list[str]) -> bool:
    if failures:
        print(f"[FAIL] {name}")
        for f in failures:
            print(f"   - {f}")
        return False
    print(f"[OK]   {name}")
    return True


def main() -> int:
    cases = [
        case_text_only,
        case_img_only,
        case_both_with_ocr_only_msg,
        case_neither,
        case_run_rejects_bare_path,
        case_verify_drops_unverified_rows,
    ]
    results = [c() for c in cases]
    passed = sum(results)
    print(f"\nSUMMARY: {passed}/{len(results)} routing cases passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
