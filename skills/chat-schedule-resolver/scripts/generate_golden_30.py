"""C4a — generate 30 synthetic Korean group-chat scenarios via Solar Pro 3.

Diversity matrix (forced — see brief for rationale):
  * 인원수: 3명(10) / 4명(15) / 5명(5)
  * 메시지 수: 5-10(15) / 11-20(15)
  * 모호 표현 포함률: 0~33%(10) / 34~66%(15) / 67%~(5)
  * 상충 의도: 없음(20) / 있음(10)
  * 주제: 학과 모임(8) / 동아리(8) / 친구(8) / 가족(3) / 회사 회식(3)

One Solar call per scenario (batching collapses diversity; we want each
scenario sampled independently). Reference date is fixed at 2026-05-11
so labelling can compute deterministic Top-3 slots.

Outputs:
  * assets/golden/golden_30.jsonl   — one scenario per line, JSON
  * assets/golden/golden_30.review.md — human-readable preview for
    the "솎아내기" review step before labeling.

Cost ~$0.30 (≈ 30 × 0.01). Below the $5 guard.
"""

from __future__ import annotations

import json
import random
import sys
import textwrap
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import requests

from upstage_client import CHAT_ENDPOINT, SOLAR_MODEL, UpstageClient


SKILL_ROOT = Path(__file__).resolve().parent.parent
GOLDEN_DIR = SKILL_ROOT / "assets" / "golden"
JSONL_PATH = GOLDEN_DIR / "golden_30.jsonl"
REVIEW_PATH = GOLDEN_DIR / "golden_30.review.md"

REFERENCE_DATE = "2026-05-11"  # Monday — anchors relative phrases


# ---- diversity matrix → 30 deterministic specs -----------------------------

@dataclass
class ScenarioSpec:
    scenario_id: str       # "p01" .. "p30"
    topic: str             # 학과 모임 / 동아리 / 친구 / 가족 / 회사 회식
    people_n: int          # 3 / 4 / 5
    msg_size: str          # "5-10" / "11-20"
    ambiguity: str         # "low" / "mid" / "high"  (0-33 / 34-66 / 67+)
    conflict: bool         # True == prefer/exclude collision present


# Counts (target):
#   topic       people_n    msg_size                   ambiguity                  conflict
#   학과(8)      3×2 4×4 5×2 — see below                                         T/F mix
#   동아리(8)    3×3 4×4 5×1
#   친구(8)      3×3 4×4 5×1
#   가족(3)      3×1 4×2
#   회사(3)      4×1 5×1 3×1
#
# Verified by COUNTS check at the bottom of this module.
SPECS: list[ScenarioSpec] = [
    # ─── 학과 모임 (8) ────────────────────────────────────────────────────
    ScenarioSpec("p01", "학과 모임", 3, "5-10", "low",  False),
    ScenarioSpec("p02", "학과 모임", 3, "11-20", "mid", False),
    ScenarioSpec("p03", "학과 모임", 4, "5-10", "mid",  True),
    ScenarioSpec("p04", "학과 모임", 4, "11-20", "low", False),
    ScenarioSpec("p05", "학과 모임", 4, "5-10", "high", True),
    ScenarioSpec("p06", "학과 모임", 4, "11-20", "mid", False),
    ScenarioSpec("p07", "학과 모임", 5, "11-20", "mid", True),
    ScenarioSpec("p08", "학과 모임", 5, "5-10", "mid",  False),
    # ─── 동아리 (8) ───────────────────────────────────────────────────────
    ScenarioSpec("p09", "동아리", 3, "5-10", "low",   False),
    ScenarioSpec("p10", "동아리", 3, "11-20", "mid",  True),
    ScenarioSpec("p11", "동아리", 3, "5-10", "high",  False),
    ScenarioSpec("p12", "동아리", 4, "11-20", "low",  False),
    ScenarioSpec("p13", "동아리", 4, "5-10", "high",  True),
    ScenarioSpec("p14", "동아리", 4, "11-20", "mid",  True),
    ScenarioSpec("p15", "동아리", 4, "11-20", "mid",  False),
    ScenarioSpec("p16", "동아리", 5, "11-20", "mid",  False),
    # ─── 친구 (8) ─────────────────────────────────────────────────────────
    ScenarioSpec("p17", "친구", 3, "5-10",  "low",  False),
    ScenarioSpec("p18", "친구", 3, "11-20", "mid",  True),
    ScenarioSpec("p19", "친구", 3, "11-20", "low",  False),
    ScenarioSpec("p20", "친구", 4, "5-10",  "mid",  True),
    ScenarioSpec("p21", "친구", 4, "11-20", "low",  False),
    ScenarioSpec("p22", "친구", 4, "11-20", "high", True),
    ScenarioSpec("p23", "친구", 4, "5-10",  "low",  False),
    ScenarioSpec("p24", "친구", 5, "5-10",  "mid",  False),
    # ─── 가족 (3) ─────────────────────────────────────────────────────────
    ScenarioSpec("p25", "가족", 3, "5-10",  "low",  False),
    ScenarioSpec("p26", "가족", 4, "11-20", "mid",  False),
    ScenarioSpec("p27", "가족", 4, "5-10",  "mid",  False),
    # ─── 회사 회식 (3) ────────────────────────────────────────────────────
    ScenarioSpec("p28", "회사 회식", 3, "5-10", "low", False),
    ScenarioSpec("p29", "회사 회식", 4, "5-10",  "high", True),
    ScenarioSpec("p30", "회사 회식", 5, "11-20", "mid", False),
]


def _verify_counts() -> None:
    from collections import Counter
    by_people = Counter(s.people_n for s in SPECS)
    by_size = Counter(s.msg_size for s in SPECS)
    by_ambig = Counter(s.ambiguity for s in SPECS)
    by_conflict = Counter(s.conflict for s in SPECS)
    by_topic = Counter(s.topic for s in SPECS)
    assert dict(by_people) == {3: 10, 4: 15, 5: 5}, by_people
    assert dict(by_size) == {"5-10": 15, "11-20": 15}, by_size
    assert dict(by_ambig) == {"low": 10, "mid": 15, "high": 5}, by_ambig
    assert dict(by_conflict) == {False: 20, True: 10}, by_conflict
    assert dict(by_topic) == {
        "학과 모임": 8, "동아리": 8, "친구": 8, "가족": 3, "회사 회식": 3,
    }, by_topic


_verify_counts()


# ---- Solar prompt ----------------------------------------------------------

AMBIG_GUIDE = {
    "low":  "전체 메시지의 0~33%만 모호 표현(예: '늦게', '이번주', '점심쯤', '주말', '평일')을 포함. 나머지는 '금요일 7시', '5월 17일 토요일 저녁 8시'처럼 구체적인 시간 표현.",
    "mid":  "전체 메시지의 34~66%가 모호 표현을 포함.",
    "high": "전체 메시지의 67% 이상이 모호 표현을 포함. 구체적인 시간 표현은 거의 없거나 매우 적음.",
}

CONFLICT_GUIDE = {
    True:  "상충 있음 — 한 사람이 선호(prefer)한 시간대를 다른 사람이 명시적으로 배제(exclude)하는 충돌이 적어도 1쌍 등장해야 함. (예: A '금요일 저녁 좋아' ↔ B '금요일은 안돼')",
    False: "상충 없음 — 발화 사이에 prefer/exclude 직접 충돌이 없는 자연스러운 흐름.",
}

TOPIC_GUIDE = {
    "학과 모임":  "학과 동기/선후배 모임. 말투는 반말 또는 가벼운 존댓말 섞임. 'OT', '종강', '시험 끝나고' 같은 학과 어휘 자연스러운 등장.",
    "동아리":    "취미 동아리(예: 밴드/사진/스터디). 반말. 동아리실/연습실/공연 같은 어휘.",
    "친구":     "가까운 친구들. 반말, 줄임말, 이모티콘 텍스트 표현(예: ㅋㅋ, ㅎㅎ) 자연스럽게.",
    "가족":     "가족 단톡(부모/형제). 반말~존댓말 혼재, '식사', '주말 본가' 같은 어휘.",
    "회사 회식":  "회사 동료/팀. 존댓말. '회식', '퇴근 후', '팀장님' 같은 어휘.",
}

SCHEMA = {
    "type": "object",
    "properties": {
        "people": {
            "type": "array",
            "description": "발화자 한국어 이름 목록 (요청한 인원수와 정확히 일치).",
            "items": {"type": "string"},
        },
        "conversation": {
            "type": "array",
            "description": "시간순 메시지 배열. 길이 = 요청한 메시지 수.",
            "items": {
                "type": "object",
                "properties": {
                    "user": {"type": "string", "description": "people 중 한 명."},
                    "text": {"type": "string", "description": "메시지 본문 — 카카오톡 자연스러운 한국어, 1~80자."},
                    "ts":   {"type": "string", "description": "오후 8:31 / 오전 11:05 같은 한국어 시각 문자열. 시간순으로 단조 증가."},
                },
                "required": ["user", "text", "ts"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["people", "conversation"],
    "additionalProperties": False,
}


def _build_messages(spec: ScenarioSpec) -> tuple[str, str]:
    msg_count_range = {
        "5-10": "5~10개",
        "11-20": "11~20개",
    }[spec.msg_size]
    system = textwrap.dedent(
        f"""\
        당신은 한국 대학생/직장인이 카카오톡 단톡방에서 실제로 약속을 잡는 대화를
        합성하는 데이터 생성기입니다. 합성된 대화는 LLM 평가 골든 데이터셋으로
        쓰이므로, "카톡스러운" 자연스러움이 핵심입니다.

        오늘은 {REFERENCE_DATE} 월요일입니다. 모든 상대 시간 표현("이번주",
        "다음주", "내일", "주말", "금요일" 등)은 이 날짜를 기준으로 합리적인
        해석이 가능해야 합니다.

        절대 제약 (반드시 지킬 것):
        - 정확히 {spec.people_n}명의 발화자를 사용.
        - 대화 길이는 {msg_count_range}.
        - 주제/상황: {TOPIC_GUIDE[spec.topic]}
        - 말투/문체: {TOPIC_GUIDE[spec.topic].split('.', 1)[1].strip() if '.' in TOPIC_GUIDE[spec.topic] else ''}
        - 모호 표현 포함률: {AMBIG_GUIDE[spec.ambiguity]}
        - 상충 의도: {CONFLICT_GUIDE[spec.conflict]}

        자연스러움 가이드 (어기면 데이터 가치 낮아짐):
        - 인사·확인 메시지(예: "ㅇㅋ", "넵", "오케이", "그래") 1~3개 자연스럽게 섞기.
        - 시간 표현이 등장하는 메시지가 최소 발화자 수 만큼은 있어야 함
          (전부가 인사로만 끝나면 안됨).
        - "오후 X:XX" 같은 ts는 메시지 흐름과 자연스럽게 일치 (몇 분~몇 시간 간격).
        - 발화자 한 명이 연속 2개 이상 메시지를 보낼 수 있음 (실제 카톡 그대로).
        - 같은 시간대를 두 명 이상이 언급하면 합의/거절 등 반응이 따라야 자연스러움.

        응답은 스키마에 정확히 맞는 JSON 객체만, 다른 설명/마크다운 없이 반환.
        """
    )
    user = (
        f"scenario_id={spec.scenario_id} ({spec.topic}, 인원={spec.people_n}, "
        f"메시지={msg_count_range}, 모호={spec.ambiguity}, 상충={spec.conflict}) "
        "을 생성하세요."
    )
    return system, user


def _generate_one(client: UpstageClient, spec: ScenarioSpec, *, timeout: float = 60.0) -> dict[str, Any]:
    system, user = _build_messages(spec)
    payload = {
        "model": SOLAR_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.7,   # variety; we don't need determinism for synthesis
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "golden_scenario",
                "strict": True,
                "schema": SCHEMA,
            },
        },
    }
    headers = {
        "Authorization": f"Bearer {client.api_key}",
        "Content-Type": "application/json",
    }
    # Light retry on transient network failures (we saw a few connection
    # resets during C3d; backoff 2/4/8s up to 3 attempts).
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


def _validate(spec: ScenarioSpec, scenario: dict[str, Any]) -> list[str]:
    """Return a list of human-readable issues (empty if valid)."""
    issues: list[str] = []
    people = scenario.get("people", [])
    if len(people) != spec.people_n:
        issues.append(f"people count {len(people)} (expected {spec.people_n})")
    convo = scenario.get("conversation", [])
    lo, hi = (5, 10) if spec.msg_size == "5-10" else (11, 20)
    if not (lo <= len(convo) <= hi):
        issues.append(f"msg count {len(convo)} (expected {lo}~{hi})")
    if convo:
        speakers = {m.get("user") for m in convo}
        unknown = speakers - set(people)
        if unknown:
            issues.append(f"speaker not in people[]: {unknown}")
    return issues


# ---- review markdown -------------------------------------------------------

def _render_review(records: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    lines.append(f"# golden_30 review (reference_date = {REFERENCE_DATE})")
    lines.append("")
    lines.append("각 시나리오를 훑으며 '카톡스럽지 않은' 케이스(과도하게 격식체, ")
    lines.append("문맥 부자연, 시간 표현 모호률 미충족 등)를 표시해 주세요. ")
    lines.append("재합성 대상 scenario_id 목록만 알려주시면 그 자리에서 재생성합니다.")
    lines.append("")
    for r in records:
        spec = r["spec"]
        s = r["scenario"]
        issues = r["issues"]
        flag = " ⚠" if issues else ""
        lines.append(f"## {spec['scenario_id']}  {spec['topic']}{flag}")
        lines.append(
            f"- people_n={spec['people_n']}  msg_size={spec['msg_size']}  "
            f"ambiguity={spec['ambiguity']}  conflict={spec['conflict']}"
        )
        if issues:
            lines.append(f"- ⚠ validation issues: {issues}")
        lines.append(f"- people: {', '.join(s.get('people', []))}")
        lines.append("")
        for i, m in enumerate(s.get("conversation", []), start=1):
            lines.append(f"  {i:>2}. **{m.get('user','?')}** ({m.get('ts','')}) — {m.get('text','')}")
        lines.append("")
    return "\n".join(lines)


# ---- driver ----------------------------------------------------------------

def main(*, dry_run: bool = False) -> int:
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    client = UpstageClient()
    records: list[dict[str, Any]] = []
    started = time.time()
    fail_count = 0
    with JSONL_PATH.open("w") as fout:
        for i, spec in enumerate(SPECS, start=1):
            print(f"[{i:>2}/30] {spec.scenario_id} ({spec.topic}, n={spec.people_n}, "
                  f"size={spec.msg_size}, ambig={spec.ambiguity}, conflict={spec.conflict})",
                  flush=True)
            if dry_run:
                continue
            try:
                scenario = _generate_one(client, spec)
            except requests.HTTPError as e:
                code = e.response.status_code if e.response is not None else "?"
                body = e.response.text[:200] if e.response is not None else ""
                print(f"   FAIL HTTP {code}: {body}")
                fail_count += 1
                continue
            except Exception as e:
                print(f"   FAIL {type(e).__name__}: {e}")
                fail_count += 1
                continue
            issues = _validate(spec, scenario)
            if issues:
                print(f"   ⚠ {issues}")
            record_line = {
                "scenario_id": spec.scenario_id,
                "spec": asdict(spec),
                "scenario": scenario,
                # placeholders for the human labeling step
                "expected_extracted": None,
                "expected_top3": None,
                "expected_unresolved": None,
            }
            fout.write(json.dumps(record_line, ensure_ascii=False) + "\n")
            fout.flush()
            records.append({
                "spec": asdict(spec),
                "scenario": scenario,
                "issues": issues,
            })
    elapsed = time.time() - started

    if not dry_run:
        REVIEW_PATH.write_text(_render_review(records), encoding="utf-8")

    print()
    print(f"Generated: {len(records)}/{len(SPECS)} ; failures: {fail_count}")
    print(f"Elapsed: {elapsed:.1f}s ; cost ≈ ${len(records) * 0.01:.2f}")
    print(f"Wrote: {JSONL_PATH}")
    print(f"Wrote: {REVIEW_PATH}")
    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main(dry_run="--dry-run" in sys.argv))
