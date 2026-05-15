"""C4a-cal — synthesize per-participant calendars for golden_30.jsonl.

Per the C4a brief: each of the 30 scenarios gets a ``calendars`` field
that maps every participant to a list of free-time windows. The
calendars are reverse-engineered against the conversation so that
labeling can produce a deterministic Top-3 ground truth in the next step.

Density matrix (forced via prompt):
  * 여유 (5)   — 8+ free hours/day, many surviving Top-3 candidates
  * 보통 (15)  — 4-6 free hours/day, a clean Top-3 emerges
  * 빡빡 (10)  — 1-3 free hours/day; intersection is tight
    └── 5 of the 10 빡빡 cases are flagged as **traps** (intentionally
        engineered so 0~1 slots survive intersection — these become the
        ``expected_unresolved`` ground truth).

Range: reference_date ±7 days  (2026-05-04 ~ 2026-05-18, 14 days inclusive).
Cost: ~$0.30 (30 Solar calls, ~$0.01/call).

Sub-commands:
    python synthesize_calendars.py --sample p01    # synth one scenario, print result
    python synthesize_calendars.py                  # synth remaining (skip those already with calendars)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import requests

from upstage_client import CHAT_ENDPOINT, SOLAR_MODEL, UpstageClient


SKILL_ROOT = Path(__file__).resolve().parent.parent
JSONL_PATH = SKILL_ROOT / "assets" / "golden" / "golden_30.jsonl"
REFERENCE_DATE = "2026-05-11"   # Monday
RANGE_START = "2026-05-04"
RANGE_END = "2026-05-18"


# ---- density / trap assignment --------------------------------------------

DENSITY_ASSIGNMENT: dict[str, str] = {
    # 여유 (5)
    "p01": "여유", "p09": "여유", "p17": "여유", "p25": "여유", "p28": "여유",
    # 빡빡 (10) — all conflict=True scenarios
    "p03": "빡빡", "p05": "빡빡", "p07": "빡빡", "p10": "빡빡", "p13": "빡빡",
    "p14": "빡빡", "p18": "빡빡", "p20": "빡빡", "p22": "빡빡", "p29": "빡빡",
    # 보통 (15) — rest
    "p02": "보통", "p04": "보통", "p06": "보통", "p08": "보통", "p11": "보통",
    "p12": "보통", "p15": "보통", "p16": "보통", "p19": "보통", "p21": "보통",
    "p23": "보통", "p24": "보통", "p26": "보통", "p27": "보통", "p30": "보통",
}
TRAP_IDS: set[str] = {"p05", "p13", "p20", "p22", "p29"}

# Sanity: counts match the C4a brief.
from collections import Counter as _C
_d = _C(DENSITY_ASSIGNMENT.values())
assert dict(_d) == {"여유": 5, "빡빡": 10, "보통": 15}, _d
assert TRAP_IDS.issubset({k for k, v in DENSITY_ASSIGNMENT.items() if v == "빡빡"})


# ---- prompt construction --------------------------------------------------

DENSITY_GUIDE = {
    "여유": (
        "각 발화자의 일간 빈 시간 총합이 평균 8시간 이상. 평일 저녁(18:00~22:00) "
        "+ 주말 오후/저녁 대부분이 빈 상태. 다양한 시간대에 후보 슬롯이 풍부해 "
        "교집합 후 Top-3 후보가 여러 개 살아남도록 합성하세요."
    ),
    "보통": (
        "각 발화자의 일간 빈 시간 총합이 평균 4~6시간. 평일 저녁 중 일부 + 주말 "
        "일부가 빈 상태. 교집합 후 Top-3 후보가 명확히 식별되도록 합성하세요."
    ),
    "빡빡": (
        "각 발화자의 일간 빈 시간 총합이 평균 1~3시간. 발화자마다 빈 시간대가 "
        "서로 잘 겹치지 않아 교집합이 작도록 합성하세요."
    ),
}

TRAP_GUIDE = (
    "이 시나리오는 **함정(unresolved) 케이스**입니다. 대화에서 합의되는 시간대를 "
    "발화자 중 1명 이상의 캘린더에서 의도적으로 비워두지 않아, 모두가 가능한 슬롯이 "
    "0개 또는 1개만 남도록 역공학 합성하세요. engineered_top3_count를 0 또는 1로 "
    "맞추고, notes에 어떤 발화자의 어떤 시간대를 차단했는지 한 줄로 기록하세요."
)

SCHEMA = {
    "type": "object",
    "properties": {
        "calendars": {
            "type": "array",
            "description": (
                "발화자별 빈 시간 윈도. 모든 시나리오 발화자가 정확히 한 번씩 등장해야 함."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "participant": {"type": "string"},
                    "windows": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "start": {
                                    "type": "string",
                                    "description": (
                                        f"ISO 8601 local 'YYYY-MM-DDTHH:MM' "
                                        f"({RANGE_START}~{RANGE_END} 범위 안)."
                                    ),
                                },
                                "end": {
                                    "type": "string",
                                    "description": (
                                        "ISO 8601 local 'YYYY-MM-DDTHH:MM', "
                                        "start보다 엄격히 큼. 24:00 금지(다음날 00:00 사용)."
                                    ),
                                },
                            },
                            "required": ["start", "end"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["participant", "windows"],
                "additionalProperties": False,
            },
        },
        "engineered_top3_count": {
            "type": "integer",
            "description": (
                "역공학으로 의도한 'Top-3 후보 슬롯 수' (모든 발화자 교집합 + "
                "exclude 차감 후 살아남는 30분 슬롯이 묶인 인터벌 개수, 0~3+)."
            ),
        },
        "notes": {
            "type": "string",
            "description": "역공학 의도를 한국어 한 줄로 설명 (예: '금요일 저녁 7~9시를 모두 비워둠').",
        },
    },
    "required": ["calendars", "engineered_top3_count", "notes"],
    "additionalProperties": False,
}


def _build_messages(scenario_id: str, scenario: dict[str, Any], density: str, is_trap: bool) -> tuple[str, str]:
    convo_lines = "\n".join(
        f"{i+1}. {m['user']} ({m.get('ts','')}): {m['text']}"
        for i, m in enumerate(scenario["conversation"])
    )
    participants = ", ".join(scenario["people"])
    trap_clause = "\n\n" + TRAP_GUIDE if is_trap else ""

    system = (
        f"당신은 단톡방 대화와 함께 사용되는 평가용 캘린더를 합성하는 엔진입니다. "
        f"오늘은 {REFERENCE_DATE} 월요일입니다. 캘린더 범위는 {RANGE_START}부터 "
        f"{RANGE_END}까지(14일).\n\n"
        f"밀도 목표: **{density}** — {DENSITY_GUIDE[density]}\n\n"
        "역공학 원칙:\n"
        "- 대화에서 prefer로 표현된 시간대는 해당 발화자의 캘린더에서 빈 시간으로 "
        "  설정 (그 발화자가 정말 가능하다고 말한 셈).\n"
        "- 대화에서 exclude로 표현된 시간대는 해당 발화자의 캘린더에서 비우지 "
        "  않아야 일관됩니다. 다만 다른 발화자의 같은 시간대는 자유롭게 결정.\n"
        "- 비함정 케이스: 합의될 법한 슬롯 1개 이상이 모든 발화자 교집합에 남아야 함.\n"
        "- start/end는 ISO 8601 로컬 'YYYY-MM-DDTHH:MM' (타임존 없음), half-open. "
        "24:00 금지 — 다음날 00:00 사용.\n"
        "- 모든 윈도가 14일 범위 안에 있어야 함.\n"
        "- 모든 발화자에 대해 정확히 한 행씩 (participant, windows[]) 출력."
        f"{trap_clause}\n\n"
        "응답은 스키마에 정확히 맞는 JSON 객체만, 다른 설명 없이 반환합니다."
    )
    user = (
        f"scenario_id={scenario_id}\n"
        f"발화자({len(scenario['people'])}명): {participants}\n\n"
        f"대화:\n{convo_lines}"
    )
    return system, user


# ---- Solar call -----------------------------------------------------------

def _generate_one(client: UpstageClient, scenario_id: str, scenario: dict[str, Any],
                  density: str, is_trap: bool, *, timeout: float = 90.0) -> dict[str, Any]:
    system, user = _build_messages(scenario_id, scenario, density, is_trap)
    payload = {
        "model": SOLAR_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.4,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "calendar_synthesis",
                "strict": True,
                "schema": SCHEMA,
            },
        },
    }
    headers = {
        "Authorization": f"Bearer {client.api_key}",
        "Content-Type": "application/json",
    }
    last: Exception | None = None
    for attempt in range(3):
        try:
            resp = requests.post(
                CHAT_ENDPOINT, headers=headers, data=json.dumps(payload),
                timeout=timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            return json.loads(data["choices"][0]["message"]["content"])
        except (requests.ConnectionError, requests.Timeout) as e:
            last = e
            time.sleep(2 * (2 ** attempt))
            continue
    raise last  # type: ignore[misc]


# ---- validation -----------------------------------------------------------

def _parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _validate(scenario: dict[str, Any], synth: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    cal = synth.get("calendars", [])
    seen: set[str] = set()
    range_lo, range_hi = _parse_iso(RANGE_START + "T00:00"), _parse_iso(RANGE_END + "T23:59")
    expected = set(scenario["people"])
    for row in cal:
        p = row["participant"]
        seen.add(p)
        for w in row.get("windows", []):
            try:
                s, e = _parse_iso(w["start"]), _parse_iso(w["end"])
            except ValueError:
                issues.append(f"{p}: bad ISO in {w}")
                continue
            if not (s < e):
                issues.append(f"{p}: start>=end {w}")
            if not (range_lo <= s and e <= range_hi + timedelta(minutes=1)):
                issues.append(f"{p}: window out of {RANGE_START}..{RANGE_END} range: {w}")
            if "T24:" in (w["start"] + w["end"]):
                issues.append(f"{p}: T24:00 used {w}")
    missing = expected - seen
    if missing:
        issues.append(f"missing participants: {missing}")
    extra = seen - expected
    if extra:
        issues.append(f"extra participants: {extra}")
    return issues


def _intersect_count(calendars_rows: list[dict]) -> int:
    """Count 30-min slots in the intersection of all participants' free time."""
    if not calendars_rows:
        return 0
    slot_sets: list[set[str]] = []
    for row in calendars_rows:
        slots: set[str] = set()
        for w in row.get("windows", []):
            try:
                s = _parse_iso(w["start"])
                e = _parse_iso(w["end"])
            except ValueError:
                continue
            t = s
            while t < e:
                slots.add(t.isoformat(timespec="minutes"))
                t += timedelta(minutes=30)
        slot_sets.append(slots)
    inter = set.intersection(*slot_sets) if slot_sets else set()
    return len(inter)


# ---- driver ---------------------------------------------------------------

def _load() -> list[dict[str, Any]]:
    return [json.loads(l) for l in JSONL_PATH.read_text().splitlines()]


def _save(records: list[dict[str, Any]]) -> None:
    JSONL_PATH.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n"
    )


def synth_one(records: list[dict[str, Any]], target_id: str, client: UpstageClient) -> tuple[dict, list[str], int]:
    rec = next(r for r in records if r["scenario_id"] == target_id)
    density = DENSITY_ASSIGNMENT[target_id]
    is_trap = target_id in TRAP_IDS
    synth = _generate_one(client, target_id, rec["scenario"], density, is_trap)
    issues = _validate(rec["scenario"], synth)
    intersect_slots = _intersect_count(synth["calendars"])
    rec["calendars"] = synth["calendars"]
    rec["calendar_synth_meta"] = {
        "density": density,
        "is_trap": is_trap,
        "engineered_top3_count": synth["engineered_top3_count"],
        "notes": synth["notes"],
        "intersect_30min_slots": intersect_slots,
        "issues": issues,
    }
    return synth, issues, intersect_slots


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", help="synth one scenario_id and print result", default=None)
    ap.add_argument("--all", action="store_true", help="synth everything missing calendars")
    args = ap.parse_args()

    records = _load()
    client = UpstageClient()

    if args.sample:
        synth, issues, intersect = synth_one(records, args.sample, client)
        _save(records)
        print(json.dumps(synth, ensure_ascii=False, indent=2))
        print()
        print(f"validation issues: {issues}")
        print(f"intersect 30-min slot count: {intersect}")
        print(f"density={DENSITY_ASSIGNMENT[args.sample]} trap={args.sample in TRAP_IDS}"
              f" engineered_top3={synth['engineered_top3_count']}")
        return 0 if not issues else 1

    if not args.all:
        print("Pass --sample <id> or --all")
        return 1

    fail = 0
    for rec in records:
        sid = rec["scenario_id"]
        if "calendars" in rec:
            print(f"{sid}: already has calendars, skip")
            continue
        try:
            synth, issues, intersect = synth_one(records, sid, client)
        except Exception as e:
            print(f"{sid}: FAIL {type(e).__name__}: {str(e)[:120]}")
            fail += 1
            continue
        flag = " ⚠ " + ",".join(issues) if issues else ""
        print(f"{sid}: density={DENSITY_ASSIGNMENT[sid]} trap={sid in TRAP_IDS} "
              f"engineered={synth['engineered_top3_count']} actual_slots={intersect}{flag}")
        _save(records)   # incremental save: partial progress preserved
    return 0 if not fail else 1


if __name__ == "__main__":
    sys.exit(main())
