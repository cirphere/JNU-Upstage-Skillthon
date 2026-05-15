"""C4c — round-trip measurement on the 30-scenario golden dataset.

For each labeled scenario, run the IE-mode pipeline end-to-end:

    conversation → synthesise PDF → IE → Solar quantization → ie_to_step2
                 → verify (Solar Pro 3 LLM-judge) → rank Top-3 over calendars

then compare the output against the human-labeled ``expected_*`` fields
to compute five primary metrics + one auxiliary:

    M1  발화자명 일치율   — recall on (who, evidence_msg_id) pairs.
    M2  메시지 본문 일치율 — output time_expr_raw substring of source msg.
    M3  순서 보존율       — output evidence_msg_id monotonic per scenario.
    M4  Top-3 정답 포함률 — expected_top3[0] ∩ output Top-3 with IoU ≥ 0.5.
    M5  환각 검출률       — F1 on (who, time_expr_raw) for unresolved sets.
    M_FP 추출 FP율        — (출력 - M1 매칭 expected) / 출력 총 수. 정보용.

Failures save raw artifacts to ``assets/golden/fail_artifacts/<id>/`` so
the demo's "0 failures" target can be patched without re-running the
expensive pipeline.

CLI:
    python measure_roundtrip.py --dry-run     # cost estimate only, no API
    python measure_roundtrip.py --run         # real run; writes measure_report.md
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from data.generate_calendar import intersection_30min


SKILL_ROOT = Path(__file__).resolve().parent.parent
JSONL_PATH = SKILL_ROOT / "assets" / "golden" / "golden_30.jsonl"
REPORT_PATH = SKILL_ROOT / "assets" / "golden" / "measure_report.md"
FAIL_ARTIFACTS_DIR = SKILL_ROOT / "assets" / "golden" / "fail_artifacts"

REFERENCE_DATE = "2026-05-11"

# Thresholds for pass/fail status in the report.
THRESHOLDS = {
    "M1": 0.95, "M2": 0.95, "M3": 0.95,
    "M4": 0.85, "M5": 0.90,
}


# ---- ranking ---------------------------------------------------------------

def _parse(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _slot_set_for_window(w: dict) -> set[str]:
    out: set[str] = set()
    t, e = _parse(w["start"]), _parse(w["end"])
    while t < e:
        out.add(t.strftime("%Y-%m-%dT%H:%M"))
        t += timedelta(minutes=30)
    return out


def _intervals_from_slots(slots: set[str]) -> list[tuple[datetime, datetime]]:
    if not slots:
        return []
    ordered = sorted(_parse(s) for s in slots)
    out: list[tuple[datetime, datetime]] = []
    run_start = ordered[0]
    prev = run_start
    for t in ordered[1:]:
        if t - prev == timedelta(minutes=30):
            prev = t
            continue
        out.append((run_start, prev + timedelta(minutes=30)))
        run_start = t
        prev = t
    out.append((run_start, prev + timedelta(minutes=30)))
    return out


def rank_top3(
    grounded_items: list[dict],
    calendars: dict[str, list[dict]],
) -> list[dict]:
    """Rank candidate meeting slots and return up to 3 step2-shape dicts.

    1. 30-min slot intersection over participant calendars.
    2. Subtract grounded exclude windows.
    3. Score surviving intervals by (1 + #prefer-supporters) × avg-certainty × duration.
    4. Tie-break by sooner-is-better; emit up to 3.
    """
    inter = intersection_30min(calendars)
    if not inter:
        return []
    for row in grounded_items:
        if row.get("type") != "exclude" or not row.get("grounded", True):
            continue
        inter -= _slot_set_for_window(row)
    prefer_rows = [
        r for r in grounded_items
        if r.get("type") == "prefer" and r.get("grounded", True)
    ]
    intervals = _intervals_from_slots(inter)
    participants = {r["who"] for r in grounded_items if "who" in r}
    scored: list[tuple[float, datetime, dict]] = []
    for s, e in intervals:
        duration_h = (e - s).total_seconds() / 3600
        supporters: list[dict] = []
        for pref in prefer_rows:
            try:
                ps, pe = _parse(pref["start"]), _parse(pref["end"])
            except (KeyError, ValueError):
                continue
            if ps < e and pe > s:
                supporters.append(pref)
        avg_cert = (sum(p.get("certainty", 0.5) for p in supporters) / len(supporters)
                    if supporters else 0.5)
        score = (1 + len(supporters)) * avg_cert * duration_h
        avail = sorted({p["who"] for p in supporters}) or sorted(participants)
        slot = {
            "start": s.strftime("%Y-%m-%dT%H:%M"),
            "end": e.strftime("%Y-%m-%dT%H:%M"),
            "participants_available": avail,
            "rationale": (
                f"{len(supporters)}개 prefer 행 지지, 평균 certainty={avg_cert:.2f}, "
                f"duration={duration_h:.1f}h."
            ),
        }
        scored.append((-score, s, slot))
    scored.sort(key=lambda x: (x[0], x[1]))
    return [s[2] for s in scored[:3]]


# ---- metric helpers --------------------------------------------------------

def _slot_iou(a: dict, b: dict) -> float:
    """Time-IoU between two half-open intervals. 0 if either is invalid."""
    try:
        as_, ae = _parse(a["start"]), _parse(a["end"])
        bs, be = _parse(b["start"]), _parse(b["end"])
    except (KeyError, ValueError):
        return 0.0
    if as_ >= ae or bs >= be:
        return 0.0
    inter_start = max(as_, bs)
    inter_end = min(ae, be)
    if inter_end <= inter_start:
        return 0.0
    inter_s = (inter_end - inter_start).total_seconds()
    union_s = (max(ae, be) - min(as_, bs)).total_seconds()
    return inter_s / union_s if union_s > 0 else 0.0


@dataclass
class PerScenarioMetrics:
    sid: str
    pipeline_ok: bool
    m1_matched: int = 0
    m1_total: int = 0
    m2_matched: int = 0
    m2_total: int = 0
    m3_monotonic: bool = True
    m3_skipped: bool = False
    m4_hit: bool | None = None     # None ⇒ no expected_top3
    m4_iou: float = 0.0            # best IoU seen against expected[0]
    m_fp_count: int = 0            # output rows not matching any expected
    m_fp_total: int = 0            # output total rows
    m5_tp: int = 0
    m5_fp: int = 0
    m5_fn: int = 0
    notes: list[str] = field(default_factory=list)


def measure_one(rec: dict, output: dict | None) -> PerScenarioMetrics:
    sid = rec["scenario_id"]
    convo = rec["scenario"]["conversation"]
    m = PerScenarioMetrics(sid=sid, pipeline_ok=output is not None)
    if output is None:
        m.notes.append("pipeline failed; metrics zeroed for this scenario")
        return m

    extracted_out = output.get("extracted", []) or []
    top3_out = output.get("top3", []) or []
    unresolved_out = output.get("unresolved", []) or []

    expected_extracted = rec.get("expected_extracted") or []
    expected_top3 = rec.get("expected_top3") or []
    expected_unresolved = rec.get("expected_unresolved") or []

    # M1 — (who, evidence_msg_id) recall
    out_keys = {(r.get("who"), r.get("evidence_msg_id")) for r in extracted_out}
    matched_out_keys: set = set()
    for exp in expected_extracted:
        m.m1_total += 1
        key = (exp.get("who"), exp.get("evidence_msg_id"))
        if key in out_keys:
            m.m1_matched += 1
            matched_out_keys.add(key)

    # M2 — output time_expr_raw substring of source msg
    for r in extracted_out:
        m.m2_total += 1
        mid = r.get("evidence_msg_id")
        phrase = r.get("time_expr_raw") or r.get("time", "")
        if not isinstance(mid, int) or not (1 <= mid <= len(convo)):
            continue
        if phrase and phrase in convo[mid - 1].get("text", ""):
            m.m2_matched += 1

    # M3 — output evidence_msg_id monotonic
    seq = [r.get("evidence_msg_id") for r in extracted_out
           if isinstance(r.get("evidence_msg_id"), int)]
    if len(seq) <= 1:
        m.m3_skipped = True
    else:
        m.m3_monotonic = all(a <= b for a, b in zip(seq, seq[1:]))

    # M4 — IoU ≥ 0.5 between expected_top3[0] and any output Top-3 slot
    if expected_top3:
        target = expected_top3[0]
        best = 0.0
        for slot in top3_out:
            iou = _slot_iou(target, slot)
            if iou > best:
                best = iou
        m.m4_iou = best
        m.m4_hit = best >= 0.5
    else:
        m.m4_hit = None

    # M5 — F1 on (who, time_expr_raw) for unresolved sets
    def _u_key(r: dict) -> tuple[str, str]:
        return (r.get("who", "*"), r.get("time_expr_raw") or r.get("time", ""))
    out_u = {_u_key(r) for r in unresolved_out}
    exp_u = {_u_key(r) for r in expected_unresolved}
    m.m5_tp = len(out_u & exp_u)
    m.m5_fp = len(out_u - exp_u)
    m.m5_fn = len(exp_u - out_u)

    # M_FP — (output rows not matching any expected by (who, evidence_msg_id)) / output total
    # Guard: if output total = 0, M_FP = 0.
    m.m_fp_total = len(extracted_out)
    if m.m_fp_total > 0:
        m.m_fp_count = sum(
            1 for r in extracted_out
            if (r.get("who"), r.get("evidence_msg_id")) not in matched_out_keys
        )
    return m


# ---- aggregate -------------------------------------------------------------

@dataclass
class FailoverStats:
    ie_count: int = 0
    failover_count: int = 0
    failover_scenarios: list[str] = field(default_factory=list)


def collect_failover(per_outputs: list[tuple[str, dict | None]]) -> FailoverStats:
    s = FailoverStats()
    for sid, out in per_outputs:
        if out is None:
            continue
        backend = out.get("backend_used")
        if backend == "ie":
            s.ie_count += 1
        elif backend == "solar_failover":
            s.failover_count += 1
            s.failover_scenarios.append(sid)
    return s


def aggregate(per: list[PerScenarioMetrics]) -> dict[str, Any]:
    def _ratio(n: int, d: int) -> float | None:
        return n / d if d else None
    m1_n = sum(x.m1_matched for x in per); m1_d = sum(x.m1_total for x in per)
    m2_n = sum(x.m2_matched for x in per); m2_d = sum(x.m2_total for x in per)
    m3_elig = [x for x in per if not x.m3_skipped]
    m3_n = sum(1 for x in m3_elig if x.m3_monotonic); m3_d = len(m3_elig)
    m4_elig = [x for x in per if x.m4_hit is not None]
    m4_n = sum(1 for x in m4_elig if x.m4_hit); m4_d = len(m4_elig)
    tp = sum(x.m5_tp for x in per); fp = sum(x.m5_fp for x in per); fn = sum(x.m5_fn for x in per)
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    m_fp_n = sum(x.m_fp_count for x in per); m_fp_d = sum(x.m_fp_total for x in per)
    return {
        "M1": (m1_n, m1_d, _ratio(m1_n, m1_d)),
        "M2": (m2_n, m2_d, _ratio(m2_n, m2_d)),
        "M3": (m3_n, m3_d, _ratio(m3_n, m3_d)),
        "M4": (m4_n, m4_d, _ratio(m4_n, m4_d)),
        "M5": {"tp": tp, "fp": fp, "fn": fn,
               "precision": precision, "recall": recall, "f1": f1},
        "M_FP": (m_fp_n, m_fp_d, _ratio(m_fp_n, m_fp_d) if m_fp_d else 0.0),
        "failures": [x.sid for x in per if not x.pipeline_ok],
    }


# ---- pipeline + fail artifacts --------------------------------------------

def _save_fail_artifacts(sid: str, *,
                         ie_raw: Any = None, quant_raw: Any = None,
                         error: str | None = None) -> None:
    out_dir = FAIL_ARTIFACTS_DIR / sid
    out_dir.mkdir(parents=True, exist_ok=True)
    if ie_raw is not None:
        (out_dir / "raw_ie_response.json").write_text(
            json.dumps(ie_raw, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    if quant_raw is not None:
        (out_dir / "raw_quantization_response.json").write_text(
            json.dumps(quant_raw, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    if error is not None:
        (out_dir / "pipeline_error_trace.txt").write_text(error, encoding="utf-8")


def run_one_real(client, rec: dict) -> dict | None:
    """Execute the IE-mode pipeline (with C5 Solar failover) against one scenario.

    Delegates to ``pipeline_ie_mode.run_with_failover`` so IE failures
    transparently fall back to Solar — the failover surface lands in
    ``output['source_notes']``. PDF synthesis errors and verify errors
    still propagate; we trap them and persist fail artifacts.
    """
    from pipeline_ie_mode import run_with_failover

    sid = rec["scenario_id"]
    scenario = rec["scenario"]
    cal = rec.get("calendars") or {}
    try:
        pipeline_out = run_with_failover(
            client,
            conversation=scenario["conversation"],
            reference_date=REFERENCE_DATE,
            title=sid,
        )
    except Exception:
        _save_fail_artifacts(sid, error=traceback.format_exc())
        return None

    verdict = pipeline_out["step3_result"]
    grounded = verdict["grounded"]
    unresolved = verdict["unresolved"]
    extracted = [
        {"who": r["who"], "type": r["type"],
         "time_expr_raw": r.get("time"),
         "evidence_msg_id": r["evidence_msg_id"],
         "grounded": r.get("grounded", True)}
        for r in grounded
    ]
    top3 = rank_top3(grounded, cal)
    return {
        "backend_used": pipeline_out["backend_used"],
        "source_notes": pipeline_out["source_notes"],
        "extracted": extracted,
        "top3": top3,
        "unresolved": [
            {"who": r["who"], "time_expr_raw": r.get("time")}
            for r in unresolved
        ],
    }


# ---- driver + report ------------------------------------------------------

def _load_records() -> list[dict]:
    return [json.loads(l) for l in JSONL_PATH.read_text().splitlines()]


def _estimate_cost(records: list[dict]) -> dict[str, Any]:
    n = len(records)
    ie_cost = n * 0.01
    quant_cost = n * 0.002
    verify_cost = n * 3 * 0.002
    total = ie_cost + quant_cost + verify_cost
    return {
        "ie_calls": n, "ie_cost": ie_cost,
        "solar_quant_calls": n, "solar_quant_cost": quant_cost,
        "solar_verify_calls_est": n * 3, "solar_verify_cost_est": verify_cost,
        "total_estimate": total,
    }


def _status(value: float | None, threshold: float) -> str:
    if value is None:
        return "—"
    return "✅" if value >= threshold else "❌"


def _format_report(agg: dict, per: list[PerScenarioMetrics]) -> str:
    lines: list[str] = []
    lines.append("# C4 Roundtrip Measurement Report")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append("| Metric | Value | Threshold | Status |")
    lines.append("|--------|-------|-----------|--------|")
    for key in ("M1", "M2", "M3", "M4"):
        n, d, r = agg[key]
        thr = THRESHOLDS[key]
        val = f"{r*100:.1f}% ({n}/{d})" if r is not None else f"— ({n}/{d})"
        lines.append(f"| {key} | {val} | {thr*100:.0f}% | {_status(r, thr)} |")
    m5 = agg["M5"]
    lines.append(
        f"| M5 (F1) | {m5['f1']*100:.1f}% (tp={m5['tp']} fp={m5['fp']} fn={m5['fn']}) | "
        f"{THRESHOLDS['M5']*100:.0f}% | {_status(m5['f1'], THRESHOLDS['M5'])} |"
    )
    fp_n, fp_d, fp_r = agg["M_FP"]
    fp_val = f"{fp_r*100:.1f}% ({fp_n}/{fp_d})" if fp_r is not None else f"— ({fp_n}/{fp_d})"
    lines.append(f"| M_FP (FP rate, info) | {fp_val} | — | — |")
    n_fail = len(agg["failures"])
    lines.append(f"| Pipeline Failures | {n_fail}/{len(per)} | 0 | {'✅' if n_fail == 0 else '❌'} |")

    fo = agg.get("failover")
    if fo and (fo.ie_count or fo.failover_count):
        lines.append("")
        lines.append("## Backend / Failover")
        lines.append(f"- IE primary: {fo.ie_count}/{len(per)}")
        lines.append(f"- Solar failover: {fo.failover_count}/{len(per)}")
        if fo.failover_scenarios:
            lines.append(f"- Failover scenarios: {', '.join(fo.failover_scenarios)}")

    if agg["failures"]:
        lines.append("")
        lines.append("## Failed Scenarios")
        for sid in agg["failures"]:
            lines.append(f"- `{sid}` — see `assets/golden/fail_artifacts/{sid}/`")

    # List sub-threshold scenarios so the user can drill in fast.
    sub_thresh = []
    for x in per:
        if x.m4_hit is False:
            sub_thresh.append((x.sid, f"M4 미매치 (best IoU={x.m4_iou:.2f})"))
    if sub_thresh:
        lines.append("")
        lines.append("## M4 missed")
        for sid, note in sub_thresh:
            lines.append(f"- {sid}: {note}")

    lines.append("")
    lines.append("## Per-scenario")
    lines.append("| id | pipeline | M1 | M2 | M3 | M4 (IoU) | M5 tp/fp/fn | M_FP | notes |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for x in per:
        m1 = f"{x.m1_matched}/{x.m1_total}" if x.m1_total else "—"
        m2 = f"{x.m2_matched}/{x.m2_total}" if x.m2_total else "—"
        m3 = "—" if x.m3_skipped else ("✓" if x.m3_monotonic else "✗")
        if x.m4_hit is None:
            m4 = "—"
        else:
            m4 = ("✓" if x.m4_hit else "✗") + f" ({x.m4_iou:.2f})"
        m5 = f"{x.m5_tp}/{x.m5_fp}/{x.m5_fn}"
        mfp = f"{x.m_fp_count}/{x.m_fp_total}" if x.m_fp_total else "—"
        notes = "; ".join(x.notes)
        lines.append(
            f"| {x.sid} | {'✓' if x.pipeline_ok else '✗'} | {m1} | {m2} | {m3} | "
            f"{m4} | {m5} | {mfp} | {notes} |"
        )
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="print cost estimate only, no API calls")
    ap.add_argument("--run", action="store_true",
                    help="execute the real round-trip (requires UPSTAGE_API_KEY)")
    args = ap.parse_args()

    records = _load_records()
    est = _estimate_cost(records)
    print("=" * 64)
    print("Cost estimate")
    print("=" * 64)
    for k, v in est.items():
        if isinstance(v, float):
            print(f"  {k:<30} ${v:.3f}")
        else:
            print(f"  {k:<30} {v}")

    if args.dry_run and not args.run:
        return 0
    if not args.run:
        print("\nPass --run to execute (or --dry-run to confirm cost only).")
        return 0

    print()
    print("=" * 64)
    print("Running real pipeline")
    print("=" * 64)
    from upstage_client import UpstageClient
    client = UpstageClient()
    per: list[PerScenarioMetrics] = []
    outputs: list[tuple[str, dict | None]] = []
    for i, rec in enumerate(records, start=1):
        sid = rec["scenario_id"]
        print(f"[{i:>2}/{len(records)}] {sid}", flush=True)
        output = run_one_real(client, rec)
        outputs.append((sid, output))
        per.append(measure_one(rec, output))
        if output and output.get("backend_used") == "solar_failover":
            print(f"      ↳ IE failover engaged: {output['source_notes'][0]}")
    agg = aggregate(per)
    agg["failover"] = collect_failover(outputs)
    report = _format_report(agg, per)
    REPORT_PATH.write_text(report)
    print()
    print(f"Wrote: {REPORT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
