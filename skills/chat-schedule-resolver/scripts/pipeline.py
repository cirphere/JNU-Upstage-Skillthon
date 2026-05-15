"""End-to-end pipeline: image|text -> grounded chat-schedule items.

Stages (each lives in its own module so it can be exercised in isolation):
    Step 4  (optional)  image(s)       -> OCR + reformat -> 'speaker: msg' lines
    Step 1              chat text      -> Solar Pro 3 free-form 1st-pass
    Step 2              + chat text    -> strict-schema normalized items
    Step 3              + chat text    -> grounded / unresolved partition

This module wires them together and exposes ``run(...)``.

Input routing (SKILL.md §3.1):
    conversation_text only -> use as-is
    image_paths only       -> OCR each path, concatenate chat_lines
    both                   -> conversation is authoritative; OCR results
                              held as "source notes" so messages that
                              appear in OCR but not in conversation can be
                              surfaced for the caller (silently merging is
                              forbidden — see SKILL.md failure modes)
    neither                -> ValueError
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from step2_normalize import normalize as step2_normalize
from step3_verify import verify_extracted_preferences
from step4_ocr_ingest import ingest_kakao_image
from upstage_client import UpstageClient


def _normalize_msg_key(user: str, text: str) -> str:
    """Whitespace-insensitive comparison key for (user, text).

    OCR tends to introduce extra spaces or drop them; using this key lets
    us cross-check OCR'd messages against the authoritative conversation
    without false positives from cosmetic whitespace differences.
    """
    return f"{''.join(user.split())}|{''.join(text.split())}"


def prepare_chat_text(
    *,
    client: UpstageClient,
    conversation_text: str | None,
    image_paths: list[str | Path] | None,
) -> dict[str, Any]:
    """Resolve the four input-routing cases into a single chat_text.

    Returns:
        {
            "chat_text": str,                # what Step 1 will see (may be "")
            "step4_results": [               # per-image OCR debug records
                {"image_path": str, "chat_lines": str, "messages": [...],
                 "ocr_raw": str},
                ...
            ],
            "source_notes": [                # cross-source diagnostics
                {"source": "ocr_only", "image_path": str,
                 "user": str, "text": str},
                ...
            ],
        }

    Raises:
        ValueError: when both conversation_text and image_paths are absent.

    The function is pure w.r.t. routing — the only side effects are the
    OCR/reformat API calls inside ``ingest_kakao_image``. Tests can stub
    the OCR layer (or skip it entirely with text-only / neither cases)
    without touching the rest of the pipeline.
    """
    has_conv = bool(conversation_text and conversation_text.strip())
    has_img = bool(image_paths)
    if not has_conv and not has_img:
        raise ValueError("Provide conversation_text or image_paths (or both).")

    step4_results: list[dict[str, Any]] = []
    if has_img:
        for p in image_paths or []:
            r = ingest_kakao_image(client, p)
            r["image_path"] = str(p)
            step4_results.append(r)

    source_notes: list[dict[str, Any]] = []
    if has_conv and has_img:
        # conversation authoritative; surface OCR-only msgs as diagnostics
        conv_keys = {
            _normalize_msg_key(*line.split(": ", 1))
            for line in (conversation_text or "").splitlines()
            if ": " in line
        }
        for r in step4_results:
            for m in r["messages"]:
                key = _normalize_msg_key(m["user"], m["text"])
                if key not in conv_keys:
                    source_notes.append(
                        {
                            "source": "ocr_only",
                            "image_path": r["image_path"],
                            "user": m["user"],
                            "text": m["text"],
                        }
                    )
        chat_text = (conversation_text or "").strip()
    elif has_conv:
        chat_text = (conversation_text or "").strip()
    else:
        # image-only path: concatenate OCR'd chat_lines in input order
        chat_text = "\n".join(
            r["chat_lines"] for r in step4_results if r["chat_lines"]
        ).strip()

    return {
        "chat_text": chat_text,
        "step4_results": step4_results,
        "source_notes": source_notes,
    }


def run(
    *,
    reference_date: str,
    conversation_text: str | None = None,
    image_paths: list[str | Path] | None = None,
    client: UpstageClient | None = None,
) -> dict[str, Any]:
    """Run the full extraction pipeline.

    Args:
        reference_date: ISO date 'YYYY-MM-DD' that anchors relative phrases.
        conversation_text: Multi-line "speaker: msg" Korean chat text. Optional.
        image_paths: List of KakaoTalk screenshot file paths. Optional. Must
            be a list — a bare ``str``/``Path`` is rejected. Use ``[path]``
            for a single image.
        client: Pre-built UpstageClient (test injection). Defaults to a
            fresh client reading UPSTAGE_API_KEY.

    Exactly one of ``conversation_text``/``image_paths`` must be provided
    (or both — see SKILL.md §3.1 for the dual-source contract).

    Returns:
        {
            "reference_date": str,
            "chat_text":      str,              # what Step 1 actually saw
            "step1_output":   str | None,       # Solar free-form
            "step2_result":   {...} | None,     # strict-schema items
            "step3_result":   {                 # evidence verification gate
                "grounded":   [...],            # rows with grounded=True
                "unresolved": [...],            # rows with grounded=False
                "verdicts":   [...],            # per (who, time) verdicts
                "notes":      [...],            # cross-source diagnostics
            },
            "step4_results":  [{...}, ...],     # per-image OCR debug
            "error":          str | None,       # set when chat_text empty
        }
    """
    if image_paths is not None and not isinstance(image_paths, list):
        raise TypeError(
            "image_paths must be a list. Wrap a single path as [path]."
        )

    client = client or UpstageClient()

    prep = prepare_chat_text(
        client=client,
        conversation_text=conversation_text,
        image_paths=image_paths,
    )
    chat_text = prep["chat_text"]
    step4_results = prep["step4_results"]
    source_notes = prep["source_notes"]

    if not chat_text:
        # OCR-only path with all images yielding empty text.
        return {
            "reference_date": reference_date,
            "chat_text": "",
            "step1_output": None,
            "step2_result": None,
            "step3_result": {
                "grounded": [],
                "unresolved": [],
                "verdicts": [],
                "notes": source_notes,
            },
            "step4_results": step4_results,
            "error": "OCR yielded no usable text",
        }

    step1_output = client.infer_time_preferences(
        chat_text, reference_date=reference_date
    )
    step2_result, _backend = step2_normalize(
        client,
        conversation_text=chat_text,
        step1_output=step1_output,
        reference_date=reference_date,
    )
    step3_result = verify_extracted_preferences(
        client,
        conversation_text=chat_text,
        step2_result=step2_result,
    )
    step3_result["notes"] = source_notes

    return {
        "reference_date": reference_date,
        "chat_text": chat_text,
        "step1_output": step1_output,
        "step2_result": step2_result,
        "step3_result": step3_result,
        "step4_results": step4_results,
        "error": None,
    }
