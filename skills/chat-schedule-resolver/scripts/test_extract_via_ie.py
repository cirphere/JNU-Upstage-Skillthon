"""Unit tests for extract_via_ie — C3c (mocked IE + mocked Solar quantization).

3 cases per the C3 brief:
    1. test_ie_basic       — normal IE response → step2-compatible output
                             after adapter (participants derived, multi-day
                             expanded via mocked resolve_time_phrases).
    2. test_ie_empty       — IE returns preferences=[] → empty result, no
                             error, no Solar call.
    3. test_ie_malformed   — IE returns schema-violating dict → 1 retry,
                             then ValueError. No silent return.

No network. Both ``UpstageClient.extract_via_ie`` and
``UpstageClient.resolve_time_phrases`` are monkeypatched.
"""

from __future__ import annotations

import sys
from typing import Any

import extract_via_ie


class _StubClient:
    """UpstageClient stand-in with canned IE + Solar batch responses.

    ``ie_responses``: list of dicts, each consumed in order on
        ``extract_via_ie`` calls. Lets a test drive the retry path.
    ``resolved_rows``: passthrough list returned from
        ``resolve_time_phrases``.
    """

    def __init__(
        self,
        ie_responses: list[dict[str, Any]],
        resolved_rows: list[dict[str, Any]] | None = None,
    ):
        self._ie_responses = list(ie_responses)
        self._resolved_rows = resolved_rows or []
        self.ie_calls = 0
        self.resolve_calls = 0
        self.last_phrases: list[str] = []

    def extract_via_ie(self, *_a, **_kw) -> dict[str, Any]:
        self.ie_calls += 1
        if not self._ie_responses:
            raise RuntimeError("stub ran out of IE responses")
        return self._ie_responses.pop(0)

    def resolve_time_phrases(
        self, phrases: list[str], *, reference_date: str
    ) -> list[dict[str, Any]]:
        self.resolve_calls += 1
        self.last_phrases = list(phrases)
        return list(self._resolved_rows)


# ---- cases -----------------------------------------------------------------

def case_ie_basic() -> bool:
    """정상 응답: 1 phrase가 multi-day로 펼쳐지면 그만큼 items 행 증가,
    participants는 unique who 순서로, time_expr_raw는 normalized output에서
    drop되어야 한다."""
    ie_response = {
        "preferences": [
            {
                "who": "민지", "type": "prefer", "time": "이번주 늦게",
                "evidence_msg_id": 1, "time_expr_raw": "이번주 늦게",
                "certainty": 0.6,
            },
            {
                "who": "준호", "type": "prefer", "time": "금요일 저녁",
                "evidence_msg_id": 2, "time_expr_raw": "금요일 저녁",
                "certainty": 0.9,
            },
            {
                "who": "지수", "type": "exclude", "time": "토요일 낮",
                "evidence_msg_id": 3, "time_expr_raw": "토요일 낮",
                "certainty": 0.9,
            },
        ]
    }
    # Mock multi-day expansion for "이번주 늦게" (3 days for brevity, real
    # would be 7), and 1 row each for the other phrases.
    resolved = [
        # "이번주 늦게" → 3 days
        {"phrase_index": 0, "start": "2026-05-11T20:00", "end": "2026-05-12T00:00"},
        {"phrase_index": 0, "start": "2026-05-12T20:00", "end": "2026-05-13T00:00"},
        {"phrase_index": 0, "start": "2026-05-13T20:00", "end": "2026-05-14T00:00"},
        # "금요일 저녁"
        {"phrase_index": 1, "start": "2026-05-15T18:00", "end": "2026-05-15T21:00"},
        # "토요일 낮"
        {"phrase_index": 2, "start": "2026-05-16T11:00", "end": "2026-05-16T16:00"},
    ]
    client = _StubClient([ie_response], resolved)
    out = extract_via_ie.ie_to_step2_format(
        client,  # type: ignore[arg-type]
        extract_via_ie.extract_preferences_from_pdf(client, b"fake-pdf"),  # type: ignore[arg-type]
        reference_date="2026-05-11",
    )

    failures = []
    # Structural shape
    if set(out.keys()) != {"participants", "items"}:
        failures.append(f"top-level keys: {sorted(out.keys())}")
    # participants order
    if out["participants"] != ["민지", "준호", "지수"]:
        failures.append(f"participants order: {out['participants']}")
    # Items count: 1 prefer "이번주 늦게" × 3 days + 1 prefer "금요일 저녁" + 1 exclude "토요일 낮" = 5
    if len(out["items"]) != 5:
        failures.append(f"items count: {len(out['items'])} (expected 5)")
    # Item keys: must match step2 SCHEMA — no time_expr_raw
    expected_keys = {"who", "type", "time", "start", "end", "certainty", "evidence_msg_id"}
    for i, it in enumerate(out["items"]):
        if set(it.keys()) != expected_keys:
            failures.append(
                f"items[{i}] keys: {sorted(it.keys())} (expected {sorted(expected_keys)})"
            )
        if "time_expr_raw" in it:
            failures.append(f"items[{i}] still has time_expr_raw")
    # evidence_msg_id mapping: 민지 rows → msg_id 1, 준호 → 2, 지수 → 3
    for it in out["items"]:
        if it["who"] == "민지" and it["evidence_msg_id"] != 1:
            failures.append(f"민지 row has wrong msg_id: {it}")
        if it["who"] == "준호" and it["evidence_msg_id"] != 2:
            failures.append(f"준호 row has wrong msg_id: {it}")
    # Solar quantization was called exactly once with unique phrases
    if client.resolve_calls != 1:
        failures.append(f"resolve_time_phrases called {client.resolve_calls}× (expected 1)")
    if client.last_phrases != ["이번주 늦게", "금요일 저녁", "토요일 낮"]:
        failures.append(f"resolve called with: {client.last_phrases}")
    return _report("ie_basic", failures)


def case_ie_empty_preferences() -> bool:
    """IE returns preferences=[] — adapter must short-circuit to empty
    result without calling Solar (no phrases to resolve)."""
    client = _StubClient([{"preferences": []}], resolved_rows=[])
    raw = extract_via_ie.extract_preferences_from_pdf(client, b"fake-pdf")  # type: ignore[arg-type]
    out = extract_via_ie.ie_to_step2_format(
        client, raw, reference_date="2026-05-11"  # type: ignore[arg-type]
    )
    failures = []
    if out != {"participants": [], "items": []}:
        failures.append(f"non-empty result: {out}")
    if client.resolve_calls != 0:
        failures.append(
            f"resolve_time_phrases must not be called for empty IE response "
            f"(got {client.resolve_calls} calls)"
        )
    return _report("ie_empty_preferences", failures)


def case_ie_malformed_response() -> bool:
    """IE returns schema-violating dict twice → ValueError after 1 retry.

    Verifies: (a) extract_via_ie does retry once, (b) the second failure
    raises ValueError, (c) no silent return of garbage downstream.
    """
    malformed = {"preferences": [{"who": "민지"}]}  # missing required keys
    # Return malformed twice (initial + 1 retry).
    client = _StubClient([malformed, malformed], resolved_rows=[])
    failures = []
    try:
        extract_via_ie.extract_preferences_from_pdf(client, b"fake-pdf")  # type: ignore[arg-type]
        failures.append("expected ValueError, got success")
    except ValueError as e:
        if "missing required keys" not in str(e):
            failures.append(f"unexpected ValueError msg: {e}")
    except Exception as e:
        failures.append(f"expected ValueError, got {type(e).__name__}: {e}")
    if client.ie_calls != 2:
        failures.append(f"expected 2 IE calls (1 retry), got {client.ie_calls}")
    return _report("ie_malformed_response", failures)


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
        case_ie_basic,
        case_ie_empty_preferences,
        case_ie_malformed_response,
    ]
    results = [c() for c in cases]
    passed = sum(results)
    print(f"\nSUMMARY: {passed}/{len(results)} extract_via_ie cases passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
