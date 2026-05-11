"""Step 2: normalize Step 1's free-form output into a strict schema.

Pipeline contract:
    in : (conversation_text, step1_output_text, reference_date)
    out: {
        "participants": [str],
        "items": [
            {
                "participant": str,
                "polarity": "prefer" | "exclude",
                "time_expr_raw": str,
                "start": ISO-8601 datetime str,   # inclusive
                "end":   ISO-8601 datetime str,   # exclusive
                "certainty": float in [0, 1],
                "source_msg_id": int (1-based line index in conversation),
            },
            ...
        ],
    }

Design choices (driven by Step 1 흠결):
  * Multi-day expressions ("다음주 점심쯤") are expanded into one item per
    (day x time-window) rather than a nested ``windows`` array. The IE
    spec forbids array-of-array, and the downstream calendar-intersection
    code is simpler when every item is a single half-open interval.
  * 24:00 is normalized to next-day 00:00 (half-open intervals).
  * source_msg_id is the 1-based line number in the conversation text so
    Step 3 (groundedness) can quote the exact utterance.

We try Information Extract first (per SKILL.md). If the server rejects
text-wrapped-as-document, we transparently fall back to Solar Pro 3
``response_format=json_schema`` and tag the result.
"""

from __future__ import annotations

import json
import sys
import textwrap
from typing import Any

import requests

from upstage_client import UpstageClient


# IE constraints honored:
#   * root is object
#   * first-level properties are array/string only (no first-level object)
#   * no array-of-array (items[] of objects, no nested array fields)
#   * all object property names listed in `required`
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "participants": {
            "type": "array",
            "description": "All speakers who appear in the conversation, in first-appearance order.",
            "items": {"type": "string"},
        },
        "items": {
            "type": "array",
            "description": (
                "One row per (speaker, single contiguous time interval). "
                "If a speaker's phrase spans multiple non-contiguous intervals "
                "(e.g. '다음주 점심쯤' = 11:30-13:30 on each weekday), emit one "
                "row per interval rather than collapsing into a wide range."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "participant": {
                        "type": "string",
                        "description": "Speaker name as it appears in the conversation."
                    },
                    "polarity": {
                        "type": "string",
                        "description": "'prefer' if the speaker wants this slot, 'exclude' if they ruled it out.",
                        "enum": ["prefer", "exclude"],
                    },
                    "time_expr_raw": {
                        "type": "string",
                        "description": "Original Korean phrase, verbatim, e.g. '이번주 늦게'.",
                    },
                    "start": {
                        "type": "string",
                        "description": (
                            "Inclusive interval start in ISO 8601 local time "
                            "(YYYY-MM-DDTHH:MM, no timezone). Use 00:00 for "
                            "all-day expressions like '주말'."
                        ),
                    },
                    "end": {
                        "type": "string",
                        "description": (
                            "Exclusive interval end in ISO 8601 local time. "
                            "Use the next day's 00:00 instead of '24:00'. "
                            "For all-day expressions, use the following midnight."
                        ),
                    },
                    "certainty": {
                        "type": "number",
                        "description": (
                            "Confidence the speaker meant this exact interval "
                            "(0.0=very vague, 1.0=explicit). Vague phrases like "
                            "'늦게' should score lower than explicit ones like "
                            "'금요일 저녁 8시'."
                        ),
                    },
                    "source_msg_id": {
                        "type": "integer",
                        "description": (
                            "1-based line number in the conversation where this "
                            "expression appeared."
                        ),
                    },
                },
                "required": [
                    "participant",
                    "polarity",
                    "time_expr_raw",
                    "start",
                    "end",
                    "certainty",
                    "source_msg_id",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["participants", "items"],
    "additionalProperties": False,
}


SYSTEM_PROMPT_TEMPLATE = """\
당신은 한국어 단체 채팅을 읽고 발화자별 시간 선호/제약을 엄격한 JSON 스키마로 정리하는 정형화기입니다.

오늘은 {reference_date}입니다. 이 날짜를 기준으로 '이번주', '다음주', '내일', '주말', '요일' 표현을 절대 날짜로 해석하세요.

입력은 두 부분입니다:
  (A) 원본 대화 — 줄 번호가 붙어 있고, 이것이 권위(source of truth)입니다.
  (B) 1차 추론 결과 — 참고용 힌트일 뿐, 누락/오류가 있을 수 있습니다. 절대 그대로 복사하지 마세요.

당신의 일은 (A)를 처음부터 끝까지 한 줄씩 다시 읽고, 시간 언급이 있는 모든 발화에 대해 items 행을 만드는 것입니다. (B)는 이미 본 적 있다고 잊지 말라는 메모일 뿐.

핵심 규칙:
1. (A)에서 시간 언급이 있는 모든 줄을 빠짐없이 처리합니다. 발화자가 3명이면 보통 3행 이상이 나옵니다. 한 줄에 표현이 여러 개면 표현마다 별도 행.
2. 다중 일자 표현은 (날짜 × 시간대) 단위로 분해합니다. 절대 여러 날을 하나의 긴 구간으로 묶지 마세요. (이 스키마는 single contiguous interval만 허용합니다.)
3. start/end는 ISO 8601 로컬 시간 'YYYY-MM-DDTHH:MM' (타임존 없음), half-open 구간(end 미포함). '24:00' 금지 — 다음날 '00:00' 사용.
4. 종일 표현은 해당 일 00:00 ~ 다음 일 00:00.
5. polarity는 'prefer' 또는 'exclude' 둘 중 하나.
6. source_msg_id = (A)의 1-기반 줄 번호.
7. 시간 언급 없는 참가자는 items에서 빠지지만 participants 배열에는 포함합니다.

정량화 규칙:
- '늦게' = 20:00 ~ 다음날 00:00
- '이른 저녁' = 18:00 ~ 19:30
- '저녁' = 18:00 ~ 21:00
- '점심' / '점심쯤' = 11:30 ~ 13:30
- '오전' = 09:00 ~ 12:00
- '오후' = 13:00 ~ 18:00
- '낮' = 11:00 ~ 16:00
- '새벽' = 00:00 ~ 06:00
- '주말' = 토요일 종일 + 일요일 종일 (2행)
- '평일' = 월~금 종일 (5행)

(B)에서 자주 나오는 실수(꼭 교정하세요):
- 'X요일 ~ Y요일'을 단일 긴 구간(start=X요일 09:00, end=Y요일 18:00)으로 묶는 경우 → 반드시 일별로 분해.
- '다음주 점심쯤'처럼 여러 날에 걸친 시간대 표현을 하나의 행으로 압축한 경우 → 해당 주 매일 11:30~13:30 행으로 분해.
- '24:00'을 그대로 사용한 경우 → 다음날 '00:00'으로 변환.

worked example (오늘 = 2026-05-11 월요일):

(A):
  1: 민지: 이번주 늦게 보자
  2: 준호: 금요일 저녁 좋아
  3: 지수: 토요일 낮은 안돼
  4: 수아: 다음주 점심쯤 어때
  5: 현우: 월요일이랑 화요일은 안돼

올바른 items (요지):
  - 민지 prefer '이번주 늦게': 월~일 매일 20:00 ~ 다음날 00:00 → 7행 (msg 1)
  - 준호 prefer '금요일 저녁': 2026-05-15T18:00 ~ 21:00 → 1행 (msg 2)
  - 지수 exclude '토요일 낮': 2026-05-16T11:00 ~ 16:00 → 1행 (msg 3)
  - 수아 prefer '다음주 점심쯤': 5/18~5/24 매일 11:30 ~ 13:30 → 7행 (msg 4)
  - 현우 exclude '월요일/화요일': 5/18 00:00~5/19 00:00, 5/19 00:00~5/20 00:00 → 2행 (msg 5)

이 예시에서 보듯이, 한 발화가 여러 날을 가리키면 반드시 날짜별로 행을 분해하고, 각 행은 단일 연속 구간만 가집니다.

응답은 스키마에 정확히 맞는 JSON 객체만, 다른 설명 없이 반환합니다.
"""


def build_user_message(conversation_text: str, step1_output: str) -> str:
    numbered = "\n".join(
        f"{i + 1}: {line}"
        for i, line in enumerate(conversation_text.splitlines())
        if line.strip()
    )
    return (
        "(A) 원본 대화 (줄 번호 포함):\n"
        f"{numbered}\n\n"
        "(B) 1차 추론 결과:\n"
        f"{step1_output}"
    )


def normalize(
    client: UpstageClient,
    *,
    conversation_text: str,
    step1_output: str,
    reference_date: str,
) -> tuple[dict[str, Any], str]:
    """Return (parsed_result, backend_used). Backend is 'ie' or 'chat_fallback'."""

    system = SYSTEM_PROMPT_TEMPLATE.format(reference_date=reference_date)
    user = build_user_message(conversation_text, step1_output)

    # IE expects a document; we wrap the combined text as a synthetic
    # text/plain "document" and pass the system instructions as the schema's
    # field descriptions + an inline preface in the user content.
    ie_input = f"{system}\n\n{user}"
    try:
        result = client.extract(ie_input, SCHEMA)
        return result, "ie"
    except requests.HTTPError as e:
        # IE may reject text-as-document. Fall back to Solar structured output.
        sys.stderr.write(
            f"[step2] IE rejected text input ({e.response.status_code}); "
            f"falling back to Solar structured output.\n"
        )
        result = client.structured_chat(
            system=system,
            user=user,
            schema=SCHEMA,
            schema_name="chat_schedule_extraction",
        )
        return result, "chat_fallback"


# ---- self-test -------------------------------------------------------------

CASES = [
    {
        "name": "SKILL.md 3-person mixed",
        "reference_date": "2026-05-11",
        "conversation": textwrap.dedent(
            """\
            민지: 이번주 늦게 보자
            준호: 금요일 저녁 좋아
            지수: 토요일 낮은 안돼
            """
        ),
        "expect_participants": {"민지", "준호", "지수"},
        "expect_min_items": 3,
    },
    {
        "name": "Multi-day lunch + weekday exclude",
        "reference_date": "2026-05-11",
        "conversation": textwrap.dedent(
            """\
            수아: 다음주 점심쯤 어때
            현우: 월요일이랑 화요일은 안돼
            """
        ),
        "expect_participants": {"수아", "현우"},
        # '다음주 점심쯤' should expand to multiple days (>= 5),
        # '월요일이랑 화요일은 안돼' should produce 2 exclude rows.
        "expect_min_items": 5,
    },
    {
        "name": "Single exclude (weekend)",
        "reference_date": "2026-05-11",
        "conversation": textwrap.dedent(
            """\
            태윤: 주말은 절대 안돼
            """
        ),
        "expect_participants": {"태윤"},
        "expect_min_items": 1,
    },
]


def _validate(result: dict[str, Any], case: dict) -> list[str]:
    failures: list[str] = []
    participants = set(result.get("participants", []))
    missing = case["expect_participants"] - participants
    if missing:
        failures.append(f"missing participants: {missing}")
    items = result.get("items", [])
    if len(items) < case["expect_min_items"]:
        failures.append(
            f"too few items: got {len(items)}, expected >= {case['expect_min_items']}"
        )
    for idx, it in enumerate(items):
        for k in (
            "participant",
            "polarity",
            "time_expr_raw",
            "start",
            "end",
            "certainty",
            "source_msg_id",
        ):
            if k not in it:
                failures.append(f"item[{idx}] missing key {k}")
        if it.get("polarity") not in ("prefer", "exclude"):
            failures.append(f"item[{idx}] bad polarity: {it.get('polarity')!r}")
        # half-open interval sanity: end > start as strings (ISO local sorts lexically)
        if it.get("start") and it.get("end") and not (it["end"] > it["start"]):
            failures.append(
                f"item[{idx}] non-increasing interval: {it['start']} -> {it['end']}"
            )
        # 24:00 must not appear
        if "T24:" in (it.get("end", "") + it.get("start", "")):
            failures.append(f"item[{idx}] T24:00 present (should be next-day T00:00)")
    return failures


def main() -> int:
    client = UpstageClient()
    overall_pass = 0
    for case in CASES:
        print("=" * 72)
        print(f"CASE: {case['name']}")
        print(f"reference_date: {case['reference_date']}")

        # Step 1 run (real API) to get free-form output
        step1_out = client.infer_time_preferences(
            case["conversation"], reference_date=case["reference_date"]
        )
        print("-- step1 output --")
        print(step1_out)

        # Step 2 normalize
        try:
            result, backend = normalize(
                client,
                conversation_text=case["conversation"],
                step1_output=step1_out,
                reference_date=case["reference_date"],
            )
        except Exception as e:
            print(f"[ERROR] {type(e).__name__}: {e}")
            continue

        print(f"-- step2 result (backend={backend}) --")
        print(json.dumps(result, ensure_ascii=False, indent=2))

        failures = _validate(result, case)
        if failures:
            print("[FAIL]")
            for f in failures:
                print(f"  - {f}")
        else:
            print("[OK] schema + interval sanity checks passed")
            overall_pass += 1

    print("=" * 72)
    print(f"SUMMARY: {overall_pass}/{len(CASES)} cases passed")
    return 0 if overall_pass == len(CASES) else 1


if __name__ == "__main__":
    sys.exit(main())
