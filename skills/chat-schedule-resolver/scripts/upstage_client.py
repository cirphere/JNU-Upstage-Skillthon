"""Upstage API client stubs for chat-schedule-resolver (v1).

Wraps the four Upstage capabilities used in the v1 pipeline:

    1. OCR                  -- KakaoTalk screenshot -> text
    2. Information Extract  -- chat text -> {participants, preferences, constraints}
    3. Solar Chat (Pro 3)   -- ambiguous Korean time expressions -> concrete windows
    4. Evidence Verification -- Solar Pro 3 as LLM-judge: verify each
                                extracted preference is supported by the chat

v2 (deferred): Embeddings -- per-user response pattern learning. See embeddings/.

All endpoints share UPSTAGE_API_KEY (loaded from assets/.env).

References (relative to skill root):
    references/upstage-ocr.md
    references/upstage-information-extract.md
    references/upstage-chat.md
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

SOLAR_MODEL = "solar-pro3"
OCR_MODEL = "ocr"


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

    def extract_via_ie(
        self,
        pdf_bytes: bytes,
        schema: dict[str, Any],
        *,
        schema_name: str = "chat_schedule_extraction",
        mime: str = "application/pdf",
        timeout: float = 120.0,
    ) -> dict[str, Any]:
        """Call Information Extract on a real document (PDF/PNG/etc.).

        This is the *honest* IE call: a binary document carried inside an
        ``image_url`` data URL with a real document MIME, as the IE spec
        requires. Unlike ``extract(text, ...)`` — which always 400s with
        "Unsupported media type" — this method receives the document bytes
        that IE was designed for (here, a one-page PDF synthesised from
        the chat by ``synthesize_pdf.synthesize_pdf``).

        Args:
            pdf_bytes: Document bytes. Default ``mime`` is PDF; pass
                ``image/png`` etc. for other formats. Must be one of IE's
                supported formats — text MIMEs will still 400.
            schema: JSON Schema for the output. Same constraints as
                ``extract``: root object, no nested arrays, ≤ 3 levels.
            schema_name: ``json_schema.name`` (≤ 64 chars, ``[a-zA-Z0-9_-]``).
            mime: Document MIME. Default ``application/pdf``.
            timeout: HTTP timeout. PDFs run slower than chat — default 120s.

        Returns:
            Parsed JSON dict matching ``schema`` (``choices[0].message.content``
            is a stringified JSON; we json.loads it before returning).

        Raises:
            requests.HTTPError: on non-2xx (caller decides whether to fall
                back to a Solar route — silent fallback is forbidden by
                the C5 failover policy).
        """
        import base64

        b64 = base64.b64encode(pdf_bytes).decode("ascii")
        payload = {
            "model": "information-extract",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{mime};base64,{b64}"
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
            "  - who: 발화자 이름\n"
            "  - type: 'prefer' (선호) 또는 'exclude' (배제)\n"
            "  - time: 원문에 등장한 한국어 표현 그대로\n"
            "  - time_resolved: 위 규칙에 따라 해석한 절대 날짜+시간 범위 (ISO 8601, "
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

    # ---- 3b. Time-phrase batch resolver (IE-mode adapter helper) -----------
    def resolve_time_phrases(
        self,
        phrases: list[str],
        *,
        reference_date: str,
        timeout: float = 60.0,
    ) -> list[dict[str, Any]]:
        """Resolve a list of Korean time phrases to ISO 8601 intervals.

        Used by IE mode (C3 α-adapter) to fill in ``start``/``end`` for
        preferences that IE extracted from a synthesised chat-table PDF.
        IE doesn't take a system prompt and can't anchor relative phrases
        ("이번주 늦게") to a reference date on its own, so we pass them to
        Solar Pro 3 in a single batch call instead.

        Multi-day expansion is intentional and matches ``step2_normalize``:
        ``"이번주 늦게"`` expands to 7 rows (Mon..Sun, each 20:00–next 00:00);
        ``"주말"`` → 2 rows (Sat + Sun); ``"평일"`` → 5 rows. One input
        phrase can produce many output rows; ``phrase_index`` is the
        0-based index back into the input list so callers can join.

        Args:
            phrases: Korean time phrases as IE extracted them.
            reference_date: ISO date ``YYYY-MM-DD`` anchoring relative
                expressions (이번주, 다음주, 내일, …).

        Returns:
            List of ``{phrase_index, start, end}`` dicts. Empty input
            short-circuits to an empty list (no API call).
        """
        if not phrases:
            return []
        schema = {
            "type": "object",
            "properties": {
                "resolved": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "phrase_index": {
                                "type": "integer",
                                "description": (
                                    "0-based index into the input phrases list. "
                                    "One phrase may yield several rows (multi-day "
                                    "expansion); they all share this index."
                                ),
                            },
                            "start": {
                                "type": "string",
                                "description": (
                                    "Inclusive interval start, ISO 8601 local "
                                    "'YYYY-MM-DDTHH:MM' (no timezone)."
                                ),
                            },
                            "end": {
                                "type": "string",
                                "description": (
                                    "Exclusive interval end, ISO 8601 local. "
                                    "Use next-day 00:00 instead of '24:00'."
                                ),
                            },
                        },
                        "required": ["phrase_index", "start", "end"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["resolved"],
            "additionalProperties": False,
        }
        system = (
            "당신은 한국어 시간 표현을 정량적 ISO 8601 로컬 구간으로 변환하는 "
            f"변환기입니다.\n\n오늘은 {reference_date}입니다. 이 날짜를 기준으로 "
            "'이번주', '다음주', '내일', '요일' 등 모든 상대 표현을 절대 날짜로 "
            "해석하세요.\n\n"
            "정량화 규칙 (반드시 적용):\n"
            "- '늦게' = 20:00 ~ 다음날 00:00\n"
            "- '이른 저녁' = 18:00 ~ 19:30\n"
            "- '저녁' = 18:00 ~ 21:00\n"
            "- '점심' / '점심쯤' = 11:30 ~ 13:30\n"
            "- '오전' = 09:00 ~ 12:00\n"
            "- '오후' = 13:00 ~ 18:00\n"
            "- '낮' = 11:00 ~ 16:00\n"
            "- '새벽' = 00:00 ~ 06:00\n"
            "- '주말' = 토요일 종일 + 일요일 종일\n"
            "- '평일' = 월~금 종일\n\n"
            "다중 일자 확장 규칙 (매우 중요):\n"
            "- '이번주 늦게' → 월~일 매일 20:00~다음날 00:00 → 7행 (모두 같은 "
            "  phrase_index)\n"
            "- '다음주 점심쯤' → 다음주 월~일 매일 11:30~13:30 → 7행\n"
            "- '주말' → 토 + 일 → 2행\n"
            "- '평일' → 월~금 → 5행\n"
            "- '금요일 저녁' → 1행 (단일 날·단일 시간대)\n\n"
            "start/end는 ISO 8601 로컬 'YYYY-MM-DDTHH:MM' (타임존 없음), "
            "half-open 구간(end 미포함). '24:00' 금지 — 다음날 '00:00' 사용.\n\n"
            "phrase_index는 입력 배열의 0-기반 인덱스. 입력 phrase가 N개라면 "
            "출력 resolved 배열의 모든 phrase_index 값은 0~N-1 범위 안에 있어야 "
            "합니다.\n\n"
            "응답은 스키마에 정확히 맞는 JSON 객체만, 다른 설명 없이 반환합니다."
        )
        user = "입력 phrases:\n" + json.dumps(phrases, ensure_ascii=False)
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
                    "name": "time_phrase_resolution",
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
            CHAT_ENDPOINT,
            headers=headers,
            data=json.dumps(payload),
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        parsed = json.loads(data["choices"][0]["message"]["content"])
        return parsed["resolved"]

    # ---- 4. Evidence Verification (Solar Pro 3 LLM-judge) ------------------
    def verify_evidence(
        self, *, context: str, claim: str, timeout: float = 30.0
    ) -> dict[str, Any]:
        """Verify a claim is supported by the source conversation.

        Solar Pro 3 acts as the LLM-judge: it sees the numbered chat
        (``context``) and one normalized claim. It must return
        ``evidence_verified=true`` only when the conversation directly
        supports the claim — invented preferences for a participant who
        never mentioned timing get ``evidence_verified=false``.

        Reasoning rules baked into the system prompt:
          * Use conversation evidence only — no outside knowledge.
          * ``supporting_msg_ids`` enumerates every 1-based line number
            whose content backs the claim (often one, but a phrase like
            "주말" may map to multiple lines if the chat repeats it).
            Empty list when the claim is unverified.
          * Boundary cases (the speaker is in the chat but the phrase
            isn't quite there) resolve to ``false`` — be conservative.

        Args:
            context: Full original conversation text. Caller numbers each
                line 1-based before passing in so ``supporting_msg_ids``
                values are interpretable.
            claim: One-line claim, typically
                ``"{who}는(은) '{time}'을(를) {선호한다|배제한다}."``.

        Returns:
            ``{"evidence_verified": bool,
               "supporting_msg_ids": [int, ...],
               "reason": str}``.
        """
        schema = {
            "type": "object",
            "properties": {
                "evidence_verified": {
                    "type": "boolean",
                    "description": (
                        "True iff the conversation directly supports the "
                        "claim. False when the speaker or phrase is absent, "
                        "or when the link is too weak to be conservative."
                    ),
                },
                "supporting_msg_ids": {
                    "type": "array",
                    "description": (
                        "1-based line numbers in the context that back the "
                        "claim. Empty list when evidence_verified is false."
                    ),
                    "items": {"type": "integer"},
                },
                "reason": {
                    "type": "string",
                    "description": "One short Korean sentence justifying the verdict.",
                },
            },
            "required": ["evidence_verified", "supporting_msg_ids", "reason"],
            "additionalProperties": False,
        }
        system = (
            "당신은 한국어 단체 채팅의 발화 기록(context)을 받아 어떤 주장(claim)이 "
            "그 발화에 의해 직접적으로 뒷받침되는지 판정하는 엄격한 판사입니다.\n\n"
            "절대 규칙:\n"
            "- context 외부 지식은 절대 사용하지 않습니다. claim이 context의 특정 "
            "  msg_id에서 직접 추론될 때만 evidence_verified=true.\n"
            "- claim의 발화자가 context에 없거나, 발화자는 있지만 해당 시간 표현을 "
            "  말한 적이 없으면 evidence_verified=false.\n"
            "- 경계 사례(애매하면)는 보수적으로 false로 판정합니다.\n\n"
            "supporting_msg_ids 규칙:\n"
            "- evidence_verified=true이면 claim을 뒷받침하는 모든 줄 번호(1-기반)를 "
            "  배열로 나열합니다. 보통 한 개, '주말' 같은 표현이 여러 줄에 반복되면 "
            "  여러 개일 수 있습니다.\n"
            "- evidence_verified=false이면 빈 배열 [].\n\n"
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
                    "name": "evidence_verification",
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
