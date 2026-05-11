"""Upstage API client stubs for chat-schedule-resolver (v1).

Wraps the four Upstage capabilities used in the v1 pipeline:

    1. OCR                  -- KakaoTalk screenshot -> text
    2. Information Extract  -- chat text -> {participants, preferences, constraints}
    3. Solar Chat (Pro 3)   -- ambiguous Korean time expressions -> concrete windows
    4. Groundedness Check   -- verify extracted preferences cite real utterances

v2 (deferred): Embeddings -- per-user response pattern learning. See embeddings/.

All endpoints share UPSTAGE_API_KEY (loaded from assets/.env). Each method below
is a stub: signature + endpoint + minimal request shape. Real implementation
lands in v1 implementation step.

References (relative to skill root):
    references/upstage-ocr.md
    references/upstage-information-extract.md
    references/upstage-chat.md
    Groundedness Check live spec: https://console.upstage.ai/api/docs/for-agents/raw
        (no local reference file -- consult live spec when implementing)
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

# Endpoints -- pinned here so callers never hardcode URLs.
OCR_ENDPOINT = "https://api.upstage.ai/v1/document-digitization"
EXTRACT_ENDPOINT = "https://api.upstage.ai/v1/information-extraction"
CHAT_ENDPOINT = "https://api.upstage.ai/v1/chat/completions"
# Groundedness Check: confirm path against the live spec before wiring up.
# https://console.upstage.ai/api/docs/for-agents/raw
GROUNDEDNESS_ENDPOINT = "https://api.upstage.ai/v1/chat/completions"  # uses chat-completions shape with model="groundedness-check"

SOLAR_MODEL = "solar-pro3"
OCR_MODEL = "ocr"
GROUNDEDNESS_MODEL = "groundedness-check"


def _load_api_key() -> str:
    """Read UPSTAGE_API_KEY from env or assets/.env. Fail fast if missing."""
    key = os.environ.get("UPSTAGE_API_KEY")
    if key:
        return key
    env_path = Path(__file__).resolve().parent.parent / "assets" / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() == "UPSTAGE_API_KEY" and v.strip():
                return v.strip()
    raise RuntimeError(
        "UPSTAGE_API_KEY not set. Copy assets/.env.example to assets/.env "
        "and fill in your key from https://console.upstage.ai"
    )


@dataclass
class UpstageClient:
    """Thin wrapper around the four v1 Upstage capabilities."""

    api_key: str | None = None

    def __post_init__(self) -> None:
        self.api_key = self.api_key or _load_api_key()

    # ---- 1. OCR --------------------------------------------------------------
    def ocr(self, image_path: str | Path, *, timeout: float = 90.0) -> str:
        """Extract text from a KakaoTalk screenshot.

        POST multipart/form-data to OCR_ENDPOINT with fields:
            model=ocr, document=<file>

        Args:
            image_path: Path to JPEG/PNG/HEIC/PDF on disk (up to 50 MB).
            timeout: HTTP timeout in seconds.

        Returns:
            Concatenated text content from the response (``text`` field).
            Layout (speaker/timestamp interleaving) is NOT preserved -- pair
            this with a reformatter (Solar Pro 3 structured output) to
            recover ``발화자: 메시지`` lines.
        """
        path = Path(image_path)
        headers = {"Authorization": f"Bearer {self.api_key}"}
        with path.open("rb") as fh:
            resp = requests.post(
                OCR_ENDPOINT,
                headers=headers,
                files={"document": (path.name, fh)},
                data={"model": OCR_MODEL},
                timeout=timeout,
            )
        resp.raise_for_status()
        return resp.json().get("text", "")

    # ---- 2. Information Extract ---------------------------------------------
    def extract(
        self,
        chat_text: str,
        schema: dict[str, Any],
        *,
        schema_name: str = "chat_schedule_extraction",
        timeout: float = 60.0,
    ) -> dict[str, Any]:
        """Pull a strict JSON object out of ``chat_text`` using the supplied schema.

        IE is officially document-based (multipart base64 of JPEG/PDF/DOCX/...).
        Plain text isn't in the supported formats list, so we wrap the text in
        a ``data:text/plain;base64,...`` data URL and try the document path.
        If the server rejects that, callers should fall back to
        ``UpstageClient.structured_chat`` (Solar Pro 3 + ``response_format``).

        Args:
            chat_text: Combined text input -- typically Step 1 output appended
                to the original conversation, so the model sees both the
                first-pass interpretation and the raw utterances.
            schema: JSON Schema object. Must satisfy IE's restrictions:
                root is object, first-level properties are scalar/array (no
                first-level object), no array-of-array, max ~3 levels deep.
            schema_name: ``json_schema.name`` (<= 64 chars, [a-zA-Z0-9_-]).

        Returns:
            Parsed JSON dict matching ``schema``.
        """
        import base64

        b64 = base64.b64encode(chat_text.encode("utf-8")).decode("ascii")
        payload = {
            "model": "information-extract",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:text/plain;base64,{b64}"
                            },
                        }
                    ],
                }
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "schema": schema},
            },
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        resp = requests.post(
            EXTRACT_ENDPOINT,
            headers=headers,
            data=json.dumps(payload),
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        return json.loads(data["choices"][0]["message"]["content"])

    def structured_chat(
        self,
        *,
        system: str,
        user: str,
        schema: dict[str, Any],
        schema_name: str = "structured_output",
        timeout: float = 60.0,
    ) -> dict[str, Any]:
        """Fallback: Solar Pro 3 with ``response_format=json_schema``.

        Same return shape as ``extract`` but uses the chat-completions
        endpoint, which accepts plain text directly. Use this when IE
        rejects text-as-document or when the input is conversational rather
        than document-shaped.
        """
        payload = {
            "model": SOLAR_MODEL,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.1,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": schema_name,
                    "strict": True,
                    "schema": schema,
                },
            },
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        resp = requests.post(
            CHAT_ENDPOINT, headers=headers, data=json.dumps(payload), timeout=timeout
        )
        resp.raise_for_status()
        data = resp.json()
        return json.loads(data["choices"][0]["message"]["content"])

    # ---- 3. Solar Chat (Pro 3) ----------------------------------------------
    def infer_time_preferences(
        self, conversation_text: str, *, reference_date: str, timeout: float = 60.0
    ) -> str:
        """First-pass extraction of per-speaker time preferences/constraints.

        Reads the entire Korean group chat at once and produces a free-form
        JSON-like dump that the next pipeline step (Information Extract) will
        normalize into a strict schema. The quantization rules below are
        baked into the system prompt so vague Korean phrases get pinned to
        concrete hour ranges in a single round-trip.

        Quantization rules (Korean -> hours; weekday refs anchored on
        ``reference_date``):
            - "늦게"            -> 20:00 ~ 24:00
            - "이른 저녁"       -> 18:00 ~ 19:30
            - "저녁"            -> 18:00 ~ 21:00
            - "점심쯤" / "점심"  -> 11:30 ~ 13:30
            - "오전"            -> 09:00 ~ 12:00
            - "오후"            -> 13:00 ~ 18:00
            - "낮"              -> 11:00 ~ 16:00
            - "새벽"            -> 00:00 ~ 06:00
            - "주말"            -> Saturday + Sunday
            - "평일"            -> Monday..Friday

        Args:
            conversation_text: Raw multi-line Korean chat (one utterance per
                line, "발화자: 내용" recommended).
            reference_date: ISO date (YYYY-MM-DD) used as "today" anchor for
                relative phrases like "이번주", "다음주", "내일".
            timeout: HTTP timeout in seconds.

        Returns:
            Assistant's raw string content. Expected to look like JSON but
            is NOT parsed here -- Step 2 (Information Extract) re-ingests
            this together with the original conversation to produce the
            strict schema.
        """
        system_prompt = (
            "당신은 한국어 단체 채팅에서 약속 시간 선호와 제약을 추출하는 분석가입니다. "
            f"오늘은 {reference_date}입니다. 이 날짜를 기준으로 '이번주', '다음주', '내일', "
            "'주말' 같은 상대 표현을 절대 날짜로 해석하세요.\n\n"
            "다음 정량화 규칙을 반드시 적용하세요:\n"
            "- '늦게' = 20:00~24:00\n"
            "- '이른 저녁' = 18:00~19:30\n"
            "- '저녁' = 18:00~21:00\n"
            "- '점심' / '점심쯤' = 11:30~13:30\n"
            "- '오전' = 09:00~12:00\n"
            "- '오후' = 13:00~18:00\n"
            "- '낮' = 11:00~16:00\n"
            "- '새벽' = 00:00~06:00\n"
            "- '주말' = 토요일+일요일\n"
            "- '평일' = 월~금\n\n"
            "출력은 각 발화자별로 다음 항목을 자유서술 JSON-유사 형태로 정리하세요:\n"
            "  - participant: 발화자 이름\n"
            "  - polarity: 'prefer' (선호) 또는 'exclude' (배제)\n"
            "  - time_expr_raw: 원문에 등장한 한국어 표현 그대로\n"
            "  - time_expr_resolved: 위 규칙에 따라 해석한 절대 날짜+시간 범위 (ISO 8601, "
            "예: 2026-05-15T20:00 ~ 2026-05-15T24:00)\n"
            "  - certainty: 0.0~1.0 (표현이 모호할수록 낮게)\n"
            "  - source_utterance: 어느 메시지에서 나왔는지 원문 한 줄\n\n"
            "시간 관련 발화가 없는 참가자는 출력에서 생략하세요. 발화자가 시간 언급을 "
            "여러 번 했다면 각각 별도 항목으로 나열하세요. 다음 단계가 이 출력을 정형화하므로 "
            "JSON 스키마를 엄격히 지키려 애쓰지 말고, 누락 없이 풍부하게 적으세요."
        )

        payload = {
            "model": SOLAR_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": conversation_text},
            ],
            "temperature": 0.2,
            "reasoning_effort": "medium",
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        resp = requests.post(
            CHAT_ENDPOINT, headers=headers, data=json.dumps(payload), timeout=timeout
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]

    # ---- 4. Groundedness Check ---------------------------------------------
    def check_groundedness(
        self, *, context: str, claim: str, timeout: float = 30.0
    ) -> dict[str, Any]:
        """Verify a claim is supported by the source context.

        Upstage's dedicated ``solar-1-mini-groundedness-check`` model is
        deprecated (returns 400 "model invalid or no longer supported"; the
        ``UpstageGroundednessCheck`` class was removed from
        ``langchain_upstage`` as of 0.7.7). We implement the same gate via
        Solar Pro 3 acting as an LLM-judge with structured JSON output.

        The judge sees the original chat (context) and a single normalized
        claim derived from a Step 2 item. It must answer ``grounded`` only
        when the context literally contains a basis for the claim --
        invented preferences for a participant who never mentioned timing
        get marked ``not_grounded``.

        Args:
            context: Full original conversation text (one utterance per line).
            claim: One-line claim, typically
                ``"{participant} {polarity} '{time_expr_raw}'"``.

        Returns:
            ``{"verdict": "grounded"|"not_grounded"|"unsure", "score": 0..1,
              "evidence_msg_id": int|null, "reason": str}``.
        """
        schema = {
            "type": "object",
            "properties": {
                "verdict": {
                    "type": "string",
                    "enum": ["grounded", "not_grounded", "unsure"],
                },
                "score": {
                    "type": "number",
                    "description": "Confidence in the verdict, 0.0..1.0.",
                },
                "evidence_msg_id": {
                    "type": ["integer", "null"],
                    "description": (
                        "1-based line number in the context whose content "
                        "supports the claim; null if not_grounded/unsure."
                    ),
                },
                "reason": {
                    "type": "string",
                    "description": "One short Korean sentence justifying the verdict.",
                },
            },
            "required": ["verdict", "score", "evidence_msg_id", "reason"],
            "additionalProperties": False,
        }
        system = (
            "당신은 한국어 단체 채팅의 발화 기록(context)을 받아 어떤 주장(claim)이 "
            "그 발화에 의해 직접적으로 뒷받침되는지 판정하는 엄격한 판사입니다.\n\n"
            "판정 규칙:\n"
            "- 'grounded': claim의 발화자와 시간 표현이 context의 특정 줄에서 "
            "  거의 그대로 발견될 때만.\n"
            "- 'not_grounded': claim의 발화자가 context에 없거나, 발화자는 있지만 "
            "  해당 시간 표현을 말한 적이 없을 때.\n"
            "- 'unsure': 명백히 한쪽으로 단정하기 어려운 경계 사례.\n\n"
            "context의 줄 번호(1-기반)는 입력에 명시되어 있습니다. grounded일 때 "
            "evidence_msg_id에 해당 줄 번호를 적습니다. 그 외에는 null.\n"
            "응답은 스키마에 정확히 맞는 JSON 객체만, 다른 설명 없이 반환합니다."
        )
        user = f"[context]\n{context}\n\n[claim]\n{claim}"
        payload = {
            "model": SOLAR_MODEL,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.0,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "groundedness_verdict",
                    "strict": True,
                    "schema": schema,
                },
            },
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        resp = requests.post(
            CHAT_ENDPOINT, headers=headers, data=json.dumps(payload), timeout=timeout
        )
        resp.raise_for_status()
        data = resp.json()
        return json.loads(data["choices"][0]["message"]["content"])


if __name__ == "__main__":
    # Smoke test: confirm key loads.
    client = UpstageClient()
    print(f"UpstageClient ready (key length: {len(client.api_key or '')})")
