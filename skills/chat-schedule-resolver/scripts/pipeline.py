"""End-to-end pipeline: image|text -> grounded chat-schedule items.

Stages (each lives in its own module so it can be exercised in isolation):
    Step 4  (optional)  image          -> OCR + reformat -> 'speaker: msg' lines
    Step 1              chat text      -> Solar Pro 3 free-form 1st-pass
    Step 2              + chat text    -> strict-schema normalized items
    Step 3              + chat text    -> grounded / unresolved partition

This module wires them together and exposes ``run(...)``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from step2_normalize import normalize as step2_normalize
from step3_filter import filter_grounded
from step4_ocr_ingest import ingest_kakao_image
from upstage_client import UpstageClient


def run(
    *,
    reference_date: str,
    conversation_text: str | None = None,
    image_path: str | Path | None = None,
    client: UpstageClient | None = None,
) -> dict[str, Any]:
    """Run the full extraction pipeline.

    Exactly one of ``conversation_text`` or ``image_path`` must be supplied.
    If both are given, the image is OCR'd and concatenated with the text
    (per SKILL.md's input contract).

    Returns a dict with the final state of each stage so callers can inspect
    intermediate outputs:

        {
            "reference_date": str,
            "chat_text":      str,              # what Step 1 actually saw
            "step1_output":   str,              # Solar free-form
            "step2_result":   {...},            # strict-schema items
            "step3_result":   {                 # groundedness gate
                "grounded":   [...],
                "unresolved": [...],
                "verdicts":   [...],
            },
            "step4_result":   {...} | None,     # OCR ingest debug, if used
        }
    """
    if conversation_text is None and image_path is None:
        raise ValueError("Provide conversation_text or image_path (or both).")

    client = client or UpstageClient()

    step4_result: dict[str, Any] | None = None
    parts: list[str] = []
    if image_path is not None:
        step4_result = ingest_kakao_image(client, image_path)
        parts.append(step4_result["chat_lines"])
    if conversation_text:
        parts.append(conversation_text.strip())
    chat_text = "\n".join(p for p in parts if p)

    step1_output = client.infer_time_preferences(
        chat_text, reference_date=reference_date
    )
    step2_result, _backend = step2_normalize(
        client,
        conversation_text=chat_text,
        step1_output=step1_output,
        reference_date=reference_date,
    )
    step3_result = filter_grounded(
        client,
        conversation_text=chat_text,
        step2_result=step2_result,
    )

    return {
        "reference_date": reference_date,
        "chat_text": chat_text,
        "step1_output": step1_output,
        "step2_result": step2_result,
        "step3_result": step3_result,
        "step4_result": step4_result,
    }
