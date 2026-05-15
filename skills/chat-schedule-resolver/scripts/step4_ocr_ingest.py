"""Step 4: KakaoTalk screenshot ingest.

OCR returns raw text in reading order with speaker labels, timestamps, and
message bubbles interleaved unpredictably. Heuristic parsing on this output
is brittle (speaker names appear once per bubble group, system messages
like '오후 8:32' weave in, emoji/sticker captions show up as garbage), so
we delegate normalization to Solar Pro 3 with strict JSON output.

Pipeline contract:
    in : (image_path, reference_date)
    out: {
        "chat_lines":  "발화자: 메시지\\n..."  # ready for Step 1
        "messages":    [{user, text, ts}, ...]  # matches docx input schema
        "ocr_raw":     str  # the OCR text we started from (kept for debug)
    }
"""

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path
from typing import Any

import requests

from upstage_client import CHAT_ENDPOINT, SOLAR_MODEL, UpstageClient


REFORMAT_SYSTEM = """\
당신은 한국어 카카오톡 단체방 스크린샷에서 OCR로 추출된 어수선한 텍스트를 받아서, 줄 단위 채팅 기록으로 정리하는 정형화기입니다.

입력 텍스트에는 다음이 섞여 있습니다:
- 발화자 이름 (각 메시지 그룹 위에 한 번씩 나오거나 매번 나옴)
- 메시지 본문 (이모티콘, 줄바꿈 포함 가능)
- 타임스탬프 ('오후 8:32', '11:05 PM' 등)
- '읽음 1', 'X명 안 읽음' 같은 UI 부속물 (메시지 아님 — 무시)
- 'YYYY년 M월 D일 X요일' 같은 날짜 헤더 (메시지 아님 — 무시)

당신의 일:
- 메시지 한 건당 하나의 message 객체를 만듭니다.
- 같은 발화자가 연속해서 보낸 여러 줄은 별개 메시지로 분리합니다.
- user는 발화자 이름. 직전 그룹의 이름을 끌어내려 보강합니다 (OCR이 매번 이름을 잡지 못해도).
- text는 메시지 본문만. 타임스탬프나 '읽음' 같은 UI 텍스트는 절대 포함하지 않습니다.
- ts는 메시지 옆에 보이는 시각 문자열을 그대로(예: '오후 8:32'). 없으면 빈 문자열.
- 메시지가 아닌 텍스트(날짜 헤더, '읽음 N', '입장하셨습니다' 등 시스템 메시지)는 messages에서 제외합니다.

응답은 스키마에 정확히 맞는 JSON 객체만 반환합니다.
"""


REFORMAT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "messages": {
            "type": "array",
            "description": "Chronologically ordered list of chat messages, oldest first.",
            "items": {
                "type": "object",
                "properties": {
                    "user": {
                        "type": "string",
                        "description": "Speaker name as displayed in the chat header.",
                    },
                    "text": {
                        "type": "string",
                        "description": "Message body only — no timestamp, no UI text.",
                    },
                    "ts": {
                        "type": "string",
                        "description": "Timestamp string next to the message, e.g. '오후 8:32'. Empty if absent.",
                    },
                },
                "required": ["user", "text", "ts"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["messages"],
    "additionalProperties": False,
}


def reformat_ocr_to_chat(client: UpstageClient, ocr_raw: str) -> dict[str, Any]:
    """Run OCR raw text through Solar Pro 3 to recover structured chat messages."""
    payload = {
        "model": SOLAR_MODEL,
        "messages": [
            {"role": "system", "content": REFORMAT_SYSTEM},
            {"role": "user", "content": ocr_raw},
        ],
        "temperature": 0.1,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "kakao_chat_reformat",
                "strict": True,
                "schema": REFORMAT_SCHEMA,
            },
        },
    }
    headers = {
        "Authorization": f"Bearer {client.api_key}",
        "Content-Type": "application/json",
    }
    resp = requests.post(
        CHAT_ENDPOINT, headers=headers, data=json.dumps(payload), timeout=60
    )
    resp.raise_for_status()
    return json.loads(resp.json()["choices"][0]["message"]["content"])


def ingest_kakao_image(
    client: UpstageClient, image_path: str | Path
) -> dict[str, Any]:
    """End-to-end Step 4: image -> structured chat lines ready for Step 1.

    Returns dict with ``chat_lines`` (str ready to feed Step 1),
    ``messages`` (list of structured records), and ``ocr_raw`` (the OCR
    text for debugging).
    """
    raw = client.ocr(image_path)
    structured = reformat_ocr_to_chat(client, raw)
    messages = structured.get("messages", [])
    chat_lines = "\n".join(
        f"{m['user']}: {m['text']}".strip() for m in messages
    )
    return {
        "chat_lines": chat_lines,
        "messages": messages,
        "ocr_raw": raw,
    }


# ---- synthetic test fixtures ----------------------------------------------

def _korean_font_path() -> str:
    candidates = [
        "/System/Library/Fonts/Supplemental/AppleGothic.ttf",
        "/System/Library/Fonts/AppleSDGothicNeo.ttc",
        "/System/Library/Fonts/Supplemental/NotoSansGothic-Regular.ttf",
    ]
    for c in candidates:
        if Path(c).exists():
            return c
    raise FileNotFoundError("No Korean font available for synthetic image generation")


def render_kakao_screenshot(
    out_path: Path,
    *,
    title: str,
    bubbles: list[tuple[str, str, str]],  # (user, text, ts)
) -> Path:
    """Render a minimal-but-realistic KakaoTalk-style screenshot for OCR tests.

    Layout:
      [title bar with chat name]
      [date header: 2026년 5월 11일 월요일]
      for each bubble:
        [small speaker label]
        [message bubble | timestamp on the right]
    """
    from PIL import Image, ImageDraw, ImageFont

    font_path = _korean_font_path()
    f_title = ImageFont.truetype(font_path, 28)
    f_name = ImageFont.truetype(font_path, 22)
    f_msg = ImageFont.truetype(font_path, 26)
    f_ts = ImageFont.truetype(font_path, 18)
    f_date = ImageFont.truetype(font_path, 20)

    width = 720
    pad_x = 32
    bubble_pad = 14
    line_gap = 18

    # Pre-compute layout y positions.
    y = 0
    height_needed = 0
    height_needed += 64  # title bar
    height_needed += 56  # date header
    for _, content, _ in bubbles:
        height_needed += 28  # name
        height_needed += 30 + 2 * bubble_pad  # bubble
        height_needed += line_gap
    height_needed += 40  # bottom pad
    height = height_needed

    img = Image.new("RGB", (width, height), "#b2c7da")  # KakaoTalk-ish backdrop
    draw = ImageDraw.Draw(img)

    # Title bar
    draw.rectangle((0, 0, width, 64), fill="#b2c7da")
    draw.text((pad_x, 18), title, fill="#000000", font=f_title)
    y = 64

    # Date header
    draw.text(
        (width // 2 - 110, y + 16),
        "2026년 5월 11일 월요일",
        fill="#333333",
        font=f_date,
    )
    y += 56

    # Bubbles (all left-aligned to avoid right-vs-left layout complexity for OCR
    # — the model still has to recover speakers from labels).
    for user, text, ts in bubbles:
        draw.text((pad_x, y), user, fill="#222222", font=f_name)
        y += 28
        # measure
        text_w = draw.textlength(text, font=f_msg)
        bubble_w = int(text_w) + 2 * bubble_pad
        bubble_h = 30 + 2 * bubble_pad
        bx0, by0 = pad_x, y
        bx1, by1 = pad_x + bubble_w, y + bubble_h
        draw.rounded_rectangle((bx0, by0, bx1, by1), radius=14, fill="white")
        draw.text((bx0 + bubble_pad, by0 + bubble_pad - 2), text, fill="#000", font=f_msg)
        if ts:
            draw.text((bx1 + 8, by1 - 22), ts, fill="#555", font=f_ts)
        y += bubble_h + line_gap

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path)
    return out_path


# ---- self-test -------------------------------------------------------------

CASES = [
    {
        "name": "3-person SKILL.md scenario",
        "title": "동기 모임",
        "bubbles": [
            ("민지", "이번주 늦게 보자", "오후 8:31"),
            ("준호", "금요일 저녁 좋아", "오후 8:33"),
            ("지수", "토요일 낮은 안돼", "오후 8:35"),
        ],
        "expect_participants": {"민지", "준호", "지수"},
        "expect_phrases": ["늦게", "금요일 저녁", "토요일"],
    },
    {
        "name": "2-person multi-day lunch",
        "title": "스터디",
        "bubbles": [
            ("수아", "다음주 점심쯤 어때", "오후 1:10"),
            ("현우", "월요일이랑 화요일은 안돼", "오후 1:12"),
        ],
        "expect_participants": {"수아", "현우"},
        "expect_phrases": ["다음주 점심쯤", "월요일", "화요일"],
    },
    {
        "name": "single speaker, multiple lines",
        "title": "회의 조율",
        "bubbles": [
            ("태윤", "주말은 절대 안돼", "오후 9:01"),
            ("태윤", "평일 저녁만 가능", "오후 9:01"),
        ],
        "expect_participants": {"태윤"},
        "expect_phrases": ["주말", "평일 저녁"],
    },
]


def _run_case(client: UpstageClient, case: dict, tmpdir: Path) -> bool:
    img_path = tmpdir / f"{case['name'].replace(' ', '_')}.png"
    render_kakao_screenshot(
        img_path, title=case["title"], bubbles=case["bubbles"]
    )
    print("=" * 72)
    print(f"CASE: {case['name']}")
    print(f"image: {img_path}")
    try:
        result = ingest_kakao_image(client, img_path)
    except Exception as e:
        print(f"[ERROR] {type(e).__name__}: {e}")
        return False
    print("-- OCR raw --")
    print(result["ocr_raw"])
    print("-- reformatted chat_lines --")
    print(result["chat_lines"])
    print("-- messages --")
    print(json.dumps(result["messages"], ensure_ascii=False, indent=2))

    failures = []
    speakers = {m["user"] for m in result["messages"]}
    missing = case["expect_participants"] - speakers
    if missing:
        failures.append(f"missing speakers: {missing}")
    for phrase in case["expect_phrases"]:
        if phrase not in result["chat_lines"]:
            failures.append(f"missing phrase: {phrase!r}")
    # No UI cruft should leak into text
    for m in result["messages"]:
        if any(tok in m["text"] for tok in ("읽음", "오후 ", "오전 ")):
            failures.append(f"UI cruft in text: {m['text']!r}")

    if failures:
        print("[FAIL]")
        for f in failures:
            print(f"  - {f}")
        return False
    print("[OK]")
    return True


def main() -> int:
    client = UpstageClient()
    tmpdir = Path(__file__).resolve().parent.parent / "assets" / "step4_tmp"
    results = [_run_case(client, c, tmpdir) for c in CASES]
    print("=" * 72)
    passed = sum(results)
    print(f"SUMMARY: {passed}/{len(results)} cases passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
