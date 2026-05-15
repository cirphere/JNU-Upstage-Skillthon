"""C3d/C3e consistency + roundtrip runner (real API calls).

Two checks:

  C3d — Consistency regression
  ----------------------------
  Run the same 3-message Korean chat through both extraction backends
  and compare:
    * Solar-only:  client.infer_time_preferences → step2_normalize
    * IE+Solar:    synthesize_pdf → extract_preferences_from_pdf →
                   ie_to_step2_format
  Targets per the C3 brief:
    * (who, type) tuples 100% match modulo extra phrases that one path
      catches and the other doesn't (informational, not a hard fail).
    * For phrases present in both, msg_id matches and start/end differ
      by ≤ 30 minutes.

  C3e — Roundtrip 1건
  --------------------
  Run IE+Solar on poc_3person.pdf (the 5-message PoC) and print the
  step2-shape output so the C3 report has a real raw JSON sample to
  attach.

Cost: ~$0.04 total (2 IE calls + 3 Solar calls).
Run: python3 scripts/test_ie_solar_consistency.py
"""

from __future__ import annotations

import json
import sys
import textwrap
from datetime import datetime, timedelta
from pathlib import Path

from extract_via_ie import (
    extract_preferences_from_pdf,
    ie_to_step2_format,
)
from step2_normalize import normalize as step2_normalize
from synthesize_pdf import synthesize_pdf
from upstage_client import UpstageClient


REFERENCE_DATE = "2026-05-11"
TOLERANCE_MIN = 30


def _iso_to_dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _within_tolerance(a: str, b: str, minutes: int = TOLERANCE_MIN) -> bool:
    try:
        delta = abs(_iso_to_dt(a) - _iso_to_dt(b))
    except ValueError:
        return False
    return delta <= timedelta(minutes=minutes)


def _summarize(items: list[dict]) -> dict[tuple[str, str], list[dict]]:
    """Group items by (who, time) for cross-path comparison."""
    out: dict[tuple[str, str], list[dict]] = {}
    for it in items:
        out.setdefault((it["who"], it["time"]), []).append(it)
    return out


# ---- C3d ------------------------------------------------------------------

CONV_TEXT = textwrap.dedent(
    """\
    민지: 이번주 늦게 보자
    준호: 금요일 저녁 좋아
    지수: 토요일 낮은 안돼
    """
).strip()

CONV_MSGS = [
    {"user": "민지", "text": "이번주 늦게 보자", "ts": ""},
    {"user": "준호", "text": "금요일 저녁 좋아", "ts": ""},
    {"user": "지수", "text": "토요일 낮은 안돼", "ts": ""},
]


def run_solar_only(client: UpstageClient) -> dict:
    s1 = client.infer_time_preferences(CONV_TEXT, reference_date=REFERENCE_DATE)
    s2, _backend = step2_normalize(
        client,
        conversation_text=CONV_TEXT,
        step1_output=s1,
        reference_date=REFERENCE_DATE,
    )
    return s2


def run_ie_plus_solar(client: UpstageClient, conversation: list[dict]) -> tuple[dict, dict]:
    pdf_bytes = synthesize_pdf(conversation, title="IE consistency test")
    ie_raw = extract_preferences_from_pdf(client, pdf_bytes)
    s2_shape = ie_to_step2_format(client, ie_raw, reference_date=REFERENCE_DATE)
    return ie_raw, s2_shape


def consistency_regression(client: UpstageClient) -> bool:
    print("=" * 72)
    print("C3d — Solar-only vs IE+Solar consistency on 3-message golden")
    print("=" * 72)

    print("-- Path A: Solar-only --")
    a = run_solar_only(client)
    print(json.dumps(a, ensure_ascii=False, indent=2)[:2000])

    print("-- Path B: IE + Solar quantization --")
    _ie_raw, b = run_ie_plus_solar(client, CONV_MSGS)
    print(json.dumps(b, ensure_ascii=False, indent=2)[:2000])

    failures = []

    # Shape match: same top-level keys, same item key set per row.
    if set(a.keys()) != set(b.keys()) or set(a.keys()) != {"participants", "items"}:
        failures.append(
            f"top-level keys differ: a={sorted(a.keys())}, b={sorted(b.keys())}"
        )
    item_keys = {"who", "type", "time", "start", "end", "certainty", "evidence_msg_id"}
    for label, dct in (("A", a), ("B", b)):
        for i, it in enumerate(dct["items"]):
            if set(it.keys()) != item_keys:
                failures.append(
                    f"path {label} items[{i}] keys: {sorted(it.keys())} "
                    f"(expected {sorted(item_keys)})"
                )
                break

    # (who, type) tuples — focus on intersection; extras are informational.
    a_pairs = {(it["who"], it["type"]) for it in a["items"]}
    b_pairs = {(it["who"], it["type"]) for it in b["items"]}
    only_a = a_pairs - b_pairs
    only_b = b_pairs - a_pairs
    if only_a or only_b:
        print(f"  (informational) only in A: {only_a} ; only in B: {only_b}")

    # For matching (who, time), compare msg_id + start/end window.
    a_summary = _summarize(a["items"])
    b_summary = _summarize(b["items"])
    common_keys = set(a_summary) & set(b_summary)
    if not common_keys:
        failures.append("no overlapping (who, time) phrases between A and B")
    for key in common_keys:
        ai = a_summary[key]
        bi = b_summary[key]
        # msg_id must match (same speaker → same row in input).
        a_msgs = {it["evidence_msg_id"] for it in ai}
        b_msgs = {it["evidence_msg_id"] for it in bi}
        if a_msgs != b_msgs:
            failures.append(
                f"msg_id mismatch for {key}: A={sorted(a_msgs)}, B={sorted(b_msgs)}"
            )
            continue
        # start/end within tolerance per row (compare expanded rows by start).
        a_starts = sorted(it["start"] for it in ai)
        b_starts = sorted(it["start"] for it in bi)
        if len(a_starts) != len(b_starts):
            print(
                f"  (informational) row count differs for {key}: "
                f"A={len(a_starts)}, B={len(b_starts)} (multi-day expansion drift)"
            )
            continue  # different cardinality — skip strict per-row comparison
        for sa, sb in zip(a_starts, b_starts):
            if not _within_tolerance(sa, sb):
                failures.append(
                    f"start drift > {TOLERANCE_MIN}min for {key}: A={sa}, B={sb}"
                )
        a_ends = sorted(it["end"] for it in ai)
        b_ends = sorted(it["end"] for it in bi)
        for ea, eb in zip(a_ends, b_ends):
            if not _within_tolerance(ea, eb):
                failures.append(
                    f"end drift > {TOLERANCE_MIN}min for {key}: A={ea}, B={eb}"
                )

    print(f"  common (who, time): {len(common_keys)}")
    print(f"  failures: {len(failures)}")
    if failures:
        for f in failures:
            print(f"    - {f}")
        return False
    print("[C3d OK]")
    return True


# ---- C3e ------------------------------------------------------------------

def roundtrip_poc(client: UpstageClient) -> dict:
    print("=" * 72)
    print("C3e — IE + Solar roundtrip on poc_3person.pdf")
    print("=" * 72)
    pdf_path = (
        Path(__file__).resolve().parent.parent
        / "assets" / "pdf_tmp" / "poc_3person.pdf"
    )
    pdf_bytes = pdf_path.read_bytes()
    ie_raw = extract_preferences_from_pdf(client, pdf_bytes)
    s2_shape = ie_to_step2_format(client, ie_raw, reference_date=REFERENCE_DATE)
    print("-- IE raw response --")
    print(json.dumps(ie_raw, ensure_ascii=False, indent=2))
    print()
    print("-- After ie_to_step2_format adapter (step2 shape) --")
    print(json.dumps(s2_shape, ensure_ascii=False, indent=2))
    return s2_shape


def main() -> int:
    client = UpstageClient()
    c3d_ok = consistency_regression(client)
    print()
    roundtrip_poc(client)
    return 0 if c3d_ok else 1


if __name__ == "__main__":
    sys.exit(main())
