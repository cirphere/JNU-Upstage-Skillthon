"""Unit tests for ``pipeline_ie_mode.run_with_failover`` (C5).

Two failover cases per the brief, plus one boundary check:
    1. test_ie_success            — IE path succeeds; backend='ie', notes=[].
    2. test_ie_failure_fallback   — extract_via_ie raises; backend flips
                                    to 'solar_failover' and a single
                                    'IE_FAILOVER: …' note appears.
    3. test_pdf_synth_no_fallback — synthesize_pdf raises; the exception
                                    propagates, no fallback, no note.

All API calls (IE, Solar quantization, Solar verify, Solar step1/step2)
are stubbed via monkeypatched module attributes. Zero network.
"""

from __future__ import annotations

import sys
from unittest.mock import patch

import pipeline_ie_mode as pim


# ---- fixtures --------------------------------------------------------------

CONV = [
    {"user": "민지", "text": "이번주 늦게 보자", "ts": ""},
    {"user": "준호", "text": "금요일 저녁 좋아", "ts": ""},
]

REF = "2026-05-11"

CANNED_IE_STEP2 = {
    "participants": ["민지", "준호"],
    "items": [
        {"who": "민지", "type": "prefer", "time": "이번주 늦게",
         "start": "2026-05-15T20:00", "end": "2026-05-16T00:00",
         "certainty": 0.7, "evidence_msg_id": 1},
        {"who": "준호", "type": "prefer", "time": "금요일 저녁",
         "start": "2026-05-15T18:00", "end": "2026-05-15T21:00",
         "certainty": 0.9, "evidence_msg_id": 2},
    ],
}

CANNED_SOLAR_STEP2 = {
    "participants": ["민지", "준호"],
    "items": [
        {"who": "민지", "type": "prefer", "time": "이번주 늦게",
         "start": "2026-05-15T20:00", "end": "2026-05-16T00:00",
         "certainty": 0.75, "evidence_msg_id": 1},
        {"who": "준호", "type": "prefer", "time": "금요일 저녁",
         "start": "2026-05-15T18:00", "end": "2026-05-15T21:00",
         "certainty": 0.85, "evidence_msg_id": 2},
    ],
}

CANNED_VERDICT = {
    "grounded": [],
    "unresolved": [],
    "verdicts": [],
}


class _StubClient:
    """Minimal client. Only the verify path is called directly on the
    instance; IE / Solar paths are intercepted at module level via patches."""

    api_key = "test"

    def verify_evidence(self, *, context, claim, **_):
        return {"evidence_verified": True, "supporting_msg_ids": [1], "reason": "stub"}

    def infer_time_preferences(self, *_a, **_kw):
        return "{stub-solar-step1}"

    def structured_chat(self, *, system, user, schema, **_kw):
        return CANNED_SOLAR_STEP2


def _report(name: str, failures: list[str]) -> bool:
    if failures:
        print(f"[FAIL] {name}")
        for f in failures:
            print(f"   - {f}")
        return False
    print(f"[OK]   {name}")
    return True


# ---- cases -----------------------------------------------------------------

def case_ie_success() -> bool:
    """Happy path — IE returns; backend='ie', no failover note."""
    with patch.object(pim, "synthesize_pdf", lambda conv, **kw: b"FAKE_PDF"), \
         patch.object(pim, "extract_preferences_from_pdf",
                      lambda client, pdf, **kw: {"preferences": []}), \
         patch.object(pim, "ie_to_step2_format",
                      lambda client, ie_raw, **kw: CANNED_IE_STEP2), \
         patch.object(pim, "verify_extracted_preferences",
                      lambda client, **kw: CANNED_VERDICT):
        out = pim.run_with_failover(
            _StubClient(), conversation=CONV, reference_date=REF, title="t",
        )
    failures = []
    if out["backend_used"] != "ie":
        failures.append(f"backend should be 'ie', got {out['backend_used']!r}")
    if out["source_notes"]:
        failures.append(f"source_notes should be empty, got {out['source_notes']}")
    if out["step2_result"] is not CANNED_IE_STEP2:
        failures.append("step2_result should be the IE-mode canned value")
    return _report("ie_success", failures)


def case_ie_failure_fallback() -> bool:
    """IE raises → backend flips to solar_failover and notes record reason."""
    def _ie_blows_up(client, pdf, **kw):
        raise RuntimeError("synthetic IE 500")

    solar_path_calls = {"n": 0}

    def _fake_solar(client, conversation, reference_date):
        solar_path_calls["n"] += 1
        return CANNED_SOLAR_STEP2

    with patch.object(pim, "synthesize_pdf", lambda conv, **kw: b"FAKE_PDF"), \
         patch.object(pim, "extract_preferences_from_pdf", _ie_blows_up), \
         patch.object(pim, "_solar_path", _fake_solar), \
         patch.object(pim, "verify_extracted_preferences",
                      lambda client, **kw: CANNED_VERDICT):
        out = pim.run_with_failover(
            _StubClient(), conversation=CONV, reference_date=REF, title="t",
        )
    failures = []
    if out["backend_used"] != "solar_failover":
        failures.append(f"backend should flip to solar_failover, got {out['backend_used']!r}")
    if len(out["source_notes"]) != 1:
        failures.append(f"expected exactly 1 source note, got {out['source_notes']}")
    elif not out["source_notes"][0].startswith("IE_FAILOVER:"):
        failures.append(f"note should start with 'IE_FAILOVER:': {out['source_notes'][0]!r}")
    elif "RuntimeError" not in out["source_notes"][0] or "synthetic IE 500" not in out["source_notes"][0]:
        failures.append(f"note should name the raised exception: {out['source_notes'][0]!r}")
    if solar_path_calls["n"] != 1:
        failures.append(f"_solar_path should be called once, got {solar_path_calls['n']}")
    if out["step2_result"] is not CANNED_SOLAR_STEP2:
        failures.append("step2_result should be the Solar canned value after failover")
    return _report("ie_failure_fallback", failures)


def case_pdf_synth_no_fallback() -> bool:
    """synthesize_pdf raises → exception propagates; no failover, no notes."""
    def _pdf_blows_up(conv, **kw):
        raise FileNotFoundError("missing NanumGothic")

    fallback_called = {"n": 0}

    def _record_fallback(client, conversation, reference_date):
        fallback_called["n"] += 1
        return CANNED_SOLAR_STEP2

    failures = []
    with patch.object(pim, "synthesize_pdf", _pdf_blows_up), \
         patch.object(pim, "_solar_path", _record_fallback):
        try:
            pim.run_with_failover(
                _StubClient(), conversation=CONV, reference_date=REF, title="t",
            )
            failures.append("expected FileNotFoundError, got success")
        except FileNotFoundError as e:
            if "NanumGothic" not in str(e):
                failures.append(f"unexpected error message: {e}")
    if fallback_called["n"] != 0:
        failures.append("PDF failure must not trigger Solar fallback (per C5 policy)")
    return _report("pdf_synth_no_fallback", failures)


# ---- runner ----------------------------------------------------------------

def main() -> int:
    cases = [
        case_ie_success,
        case_ie_failure_fallback,
        case_pdf_synth_no_fallback,
    ]
    results = [c() for c in cases]
    passed = sum(results)
    print(f"\nSUMMARY: {passed}/{len(results)} pipeline_ie_mode cases passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
