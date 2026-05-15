"""Validate the human-labeled fields in golden_30.jsonl.

Run after labeling to catch:
  * missing expected_extracted / expected_top3 / expected_unresolved fields,
  * shape errors (wrong types, bad ISO timestamps),
  * coherence errors (Top-3 slot not in calendar intersection,
    evidence_msg_id out of range, time_expr_raw not in source msg, …),
  * trap-case sanity (trap scenarios must have non-empty expected_unresolved).

CLI:
    python validate_golden.py                  # full report
    python validate_golden.py --strict         # exit non-zero on any issue
    python validate_golden.py --only p07,p13   # subset
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from data.generate_calendar import intersection_30min


SKILL_ROOT = Path(__file__).resolve().parent.parent
JSONL_PATH = SKILL_ROOT / "assets" / "golden" / "golden_30.jsonl"


def _parse(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _is_aligned_30min(s: str) -> bool:
    try:
        t = _parse(s)
    except ValueError:
        return False
    return t.minute % 30 == 0 and t.second == 0


def _check_extracted(rec: dict, issues: list[str]) -> None:
    sid = rec["scenario_id"]
    extracted = rec.get("expected_extracted")
    if extracted is None:
        issues.append(f"{sid}: expected_extracted missing (None)")
        return
    if not isinstance(extracted, list):
        issues.append(f"{sid}: expected_extracted must be a list")
        return
    convo = rec["scenario"]["conversation"]
    participants = set(rec["scenario"]["people"])
    for i, row in enumerate(extracted):
        prefix = f"{sid}: expected_extracted[{i}]"
        for k in ("who", "type", "time_expr_raw", "evidence_msg_id"):
            if k not in row:
                issues.append(f"{prefix} missing key {k!r}")
        if row.get("who") not in participants:
            issues.append(f"{prefix} who={row.get('who')!r} not in scenario participants {sorted(participants)}")
        if row.get("type") not in ("prefer", "exclude"):
            issues.append(f"{prefix} type={row.get('type')!r} not prefer|exclude")
        mid = row.get("evidence_msg_id")
        if not isinstance(mid, int) or not (1 <= mid <= len(convo)):
            issues.append(f"{prefix} evidence_msg_id={mid!r} out of 1..{len(convo)}")
            continue
        phrase = row.get("time_expr_raw", "")
        msg_text = convo[mid - 1].get("text", "")
        if phrase and phrase not in msg_text:
            issues.append(
                f"{prefix} time_expr_raw {phrase!r} not in source msg {mid}: {msg_text!r}"
            )


def _check_top3(rec: dict, issues: list[str]) -> None:
    sid = rec["scenario_id"]
    top3 = rec.get("expected_top3")
    if top3 is None:
        issues.append(f"{sid}: expected_top3 missing (None)")
        return
    if not isinstance(top3, list):
        issues.append(f"{sid}: expected_top3 must be a list")
        return
    if len(top3) > 3:
        issues.append(f"{sid}: expected_top3 has {len(top3)} rows (> 3)")
    cal = rec.get("calendars") or {}
    inter = intersection_30min(cal) if cal else set()
    participants = set(rec["scenario"]["people"])
    for i, slot in enumerate(top3):
        prefix = f"{sid}: expected_top3[{i}]"
        for k in ("start", "end", "participants_available", "rationale"):
            if k not in slot:
                issues.append(f"{prefix} missing key {k!r}")
        for k in ("start", "end"):
            v = slot.get(k, "")
            if not _is_aligned_30min(v):
                issues.append(f"{prefix}.{k}={v!r} bad ISO or not 30-min aligned")
        try:
            s, e = _parse(slot["start"]), _parse(slot["end"])
            if s >= e:
                issues.append(f"{prefix}: start>=end ({s} >= {e})")
        except (KeyError, ValueError):
            continue   # already flagged above
        avail = slot.get("participants_available", [])
        if not set(avail).issubset(participants):
            issues.append(
                f"{prefix}: participants_available {avail} ⊄ scenario participants {sorted(participants)}"
            )
        # Slot must lie within the calendar intersection on the 30-min grid.
        s = _parse(slot["start"])
        e = _parse(slot["end"])
        t = s
        missing = []
        while t < e:
            label = t.strftime("%Y-%m-%dT%H:%M")
            if label not in inter:
                missing.append(label)
            t += timedelta(minutes=30)
        if missing:
            issues.append(
                f"{prefix}: slot {slot['start']}..{slot['end']} not fully in calendar "
                f"intersection (missing 30-min ticks: {missing[:3]}{'…' if len(missing)>3 else ''})"
            )


def _check_unresolved(rec: dict, issues: list[str]) -> None:
    sid = rec["scenario_id"]
    unresolved = rec.get("expected_unresolved")
    if unresolved is None:
        issues.append(f"{sid}: expected_unresolved missing (None)")
        return
    if not isinstance(unresolved, list):
        issues.append(f"{sid}: expected_unresolved must be a list")
        return
    # Trap scenarios must record at least one expected unresolved row.
    meta = rec.get("calendar_meta", {})
    if meta.get("is_trap_effective") and not unresolved:
        issues.append(
            f"{sid}: trap=True but expected_unresolved is empty — at least one "
            "intentional hallucination/no-overlap case must be labeled"
        )
    participants = set(rec["scenario"]["people"])
    for i, row in enumerate(unresolved):
        prefix = f"{sid}: expected_unresolved[{i}]"
        if "who" in row and row["who"] not in participants and row["who"] != "*":
            issues.append(f"{prefix}.who={row.get('who')!r} not in participants (use '*' for cross-cutting)")


def validate(only: set[str] | None = None) -> tuple[list[str], int, int]:
    records = [json.loads(l) for l in JSONL_PATH.read_text().splitlines()]
    if only is not None:
        records = [r for r in records if r["scenario_id"] in only]
    issues: list[str] = []
    n_ok = 0
    for rec in records:
        before = len(issues)
        _check_extracted(rec, issues)
        _check_top3(rec, issues)
        _check_unresolved(rec, issues)
        if len(issues) == before:
            n_ok += 1
    return issues, n_ok, len(records)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true",
                    help="exit non-zero on any validation issue")
    ap.add_argument("--only", default=None,
                    help="comma-separated scenario_ids to validate")
    args = ap.parse_args()
    only = set(args.only.split(",")) if args.only else None

    issues, n_ok, n_total = validate(only)
    for line in issues:
        print(line)
    print()
    print(f"clean: {n_ok}/{n_total}  issues: {len(issues)}")
    if args.strict and issues:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
