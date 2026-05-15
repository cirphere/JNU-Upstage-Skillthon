"""IE-mode extraction: synthesised PDF → preferences via Information Extract.

Pipeline-mode wrapper that pairs ``synthesize_pdf`` with
``UpstageClient.extract_via_ie``. The chat-schedule pipeline can switch
between two extraction backends:

  * **Solar mode** (default): chat text → Solar Pro 3 structured output
    → ``step2_normalize`` items.
  * **IE mode** (this module): chat text → synthesised PDF table →
    Information Extract → preferences. Better at layout-aware speaker /
    msg_id boundary detection because IE was built for documents.

IE mode is *not* fully equivalent on its own — IE produces raw time
phrases (``time``) and the source row id (``evidence_msg_id``) but does
not resolve relative phrases ("이번주 늦게") to ISO intervals (``start``/
``end``). The pipeline needs a second pass (Solar Pro 3 quantization)
before items are usable by Step 3. The adapter design lives in C3d /
``ie_to_step2_format``.

Schema design notes
-------------------
The schema below mirrors the synthesised PDF table directly: each
"preference" item references the ``msg_id`` column of the row it was
extracted from, so IE can use its layout-understanding to anchor every
preference to a specific table row rather than guessing from prose.
"""

from __future__ import annotations

from typing import Any

from upstage_client import UpstageClient


# IE schema: matches the synthesised PDF table (msg_id | speaker |
# timestamp | text). start/end are intentionally omitted — they require
# relative-date resolution that IE can't reliably do without a system
# prompt. A Solar Pro 3 quantization pass resolves them downstream.
IE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "preferences": {
            "type": "array",
            "description": (
                "Every time preference or constraint found in the chat. "
                "Each item corresponds to one speaker's mention of a time. "
                "If a single message contains multiple distinct time "
                "expressions, emit one item per expression (do not merge)."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "who": {
                        "type": "string",
                        "description": (
                            "Speaker name, taken verbatim from the 'speaker' "
                            "column of the row this preference came from."
                        ),
                    },
                    "type": {
                        "type": "string",
                        "enum": ["prefer", "exclude"],
                        "description": (
                            "'prefer' when the message expresses availability "
                            "or wanting that time (markers: '좋아', '어때', "
                            "'~쯤', '가능'); 'exclude' when it expresses "
                            "unavailability (markers: '안돼', '없음', '바빠', "
                            "'못 가')."
                        ),
                    },
                    "time": {
                        "type": "string",
                        "description": (
                            "Korean time phrase as written in the 'text' "
                            "column. Examples: '이번주 늦게', '금요일 저녁', "
                            "'토요일 낮', '다음주 점심쯤'."
                        ),
                    },
                    "evidence_msg_id": {
                        "type": "integer",
                        "description": (
                            "The integer value from the 'msg_id' column of "
                            "the table row this preference was extracted "
                            "from. Use the column value, not a synthesised "
                            "index."
                        ),
                    },
                    "time_expr_raw": {
                        "type": "string",
                        "description": (
                            "Same as 'time' — the raw Korean phrase. Kept "
                            "as a separate field for adapter compatibility "
                            "with the Solar-mode item shape."
                        ),
                    },
                    "certainty": {
                        "type": "number",
                        "description": (
                            "0.0–1.0. Lower for vague phrases like '늦게' or "
                            "'적당히'; higher for explicit ones like '금요일 "
                            "오후 8시'."
                        ),
                    },
                },
                "required": [
                    "who",
                    "type",
                    "time",
                    "evidence_msg_id",
                    "time_expr_raw",
                    "certainty",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["preferences"],
    "additionalProperties": False,
}


REQUIRED_IE_ITEM_KEYS = {
    "who",
    "type",
    "time",
    "evidence_msg_id",
    "time_expr_raw",
    "certainty",
}


def _validate_ie_shape(parsed: Any) -> None:
    """Strict shape check on an IE response. Raises ValueError on mismatch.

    The IE server enforces the schema on its side, but defensive validation
    here protects the rest of the pipeline from silently propagating a
    malformed dict (e.g., if the server adapter ever changes).
    """
    if not isinstance(parsed, dict):
        raise ValueError(f"IE response is not a dict: {type(parsed).__name__}")
    if "preferences" not in parsed or not isinstance(parsed["preferences"], list):
        raise ValueError("IE response missing 'preferences' list")
    for i, item in enumerate(parsed["preferences"]):
        if not isinstance(item, dict):
            raise ValueError(f"preferences[{i}] not a dict: {item!r}")
        missing = REQUIRED_IE_ITEM_KEYS - set(item)
        if missing:
            raise ValueError(
                f"preferences[{i}] missing required keys: {sorted(missing)}"
            )
        if item["type"] not in ("prefer", "exclude"):
            raise ValueError(
                f"preferences[{i}].type invalid: {item['type']!r}"
            )


def extract_preferences_from_pdf(
    client: UpstageClient,
    pdf_bytes: bytes,
    *,
    schema: dict[str, Any] | None = None,
    timeout: float = 120.0,
    max_retries: int = 1,
) -> dict[str, Any]:
    """Call IE on a synthesised chat-table PDF.

    Returns IE's parsed JSON as-is after shape validation:

        {"preferences": [{who, type, time, evidence_msg_id,
                          time_expr_raw, certainty}, ...]}

    Retries once on shape violations (server adapter glitch); HTTP errors
    propagate immediately because the C5 failover policy forbids silent
    fallback — the caller decides whether to retry, fall back, or fail.

    Raises:
        requests.HTTPError: any IE HTTP error (caller routes via C5).
        ValueError: response shape violates the IE contract after retries.
    """
    last_err: Exception | None = None
    for attempt in range(max_retries + 1):
        parsed = client.extract_via_ie(
            pdf_bytes,
            schema or IE_SCHEMA,
            schema_name="chat_schedule_extraction",
            timeout=timeout,
        )
        try:
            _validate_ie_shape(parsed)
            return parsed
        except ValueError as e:
            last_err = e
            if attempt < max_retries:
                continue
            raise
    # Unreachable, but keeps the type-checker happy.
    raise last_err  # type: ignore[misc]


def ie_to_step2_format(
    client: UpstageClient,
    ie_response: dict[str, Any],
    *,
    reference_date: str,
) -> dict[str, Any]:
    """Adapter: IE-mode response → ``step2_normalize`` output format.

    Steps:
      1. Extract unique time phrases from IE preferences (preserve order).
      2. Call Solar Pro 3 batch quantization → ``[{phrase_index, start,
         end}, ...]``. One phrase can yield multiple rows (multi-day
         expansion); rows share the same ``phrase_index``.
      3. Flat-map IE preferences × resolved rows: each IE preference
         becomes 1+ output items, one per resolution row.
      4. Derive ``participants`` from unique ``who`` values (first-
         appearance order).
      5. Drop ``time_expr_raw`` per the C3 contract (raw is preserved in
         the original ``ie_response`` for audit; the normalised output
         keeps only ``time``).

    Returns a dict identical in *shape* to ``step2_normalize`` output:

        {"participants": [str, ...],
         "items": [{who, type, time, start, end, certainty,
                    evidence_msg_id}, ...]}
    """
    preferences = ie_response.get("preferences", [])
    if not preferences:
        return {"participants": [], "items": []}

    # 1. Deduplicate phrases, preserving order of first appearance.
    unique_phrases: list[str] = []
    seen_phrase_idx: dict[str, int] = {}
    for p in preferences:
        phrase = p["time"]
        if phrase not in seen_phrase_idx:
            seen_phrase_idx[phrase] = len(unique_phrases)
            unique_phrases.append(phrase)

    # 2. Batch-resolve.
    resolved_rows = client.resolve_time_phrases(
        unique_phrases, reference_date=reference_date
    )

    # Index resolved rows by phrase_index for the flat-map.
    by_idx: dict[int, list[dict[str, Any]]] = {}
    for r in resolved_rows:
        by_idx.setdefault(r["phrase_index"], []).append(r)

    # 3. Build step2-shaped items.
    items: list[dict[str, Any]] = []
    participants_order: list[str] = []
    seen_participants: set[str] = set()
    for p in preferences:
        if p["who"] not in seen_participants:
            seen_participants.add(p["who"])
            participants_order.append(p["who"])
        phrase_idx = seen_phrase_idx[p["time"]]
        for r in by_idx.get(phrase_idx, []):
            items.append(
                {
                    "who": p["who"],
                    "type": p["type"],
                    "time": p["time"],
                    "start": r["start"],
                    "end": r["end"],
                    "certainty": p["certainty"],
                    "evidence_msg_id": p["evidence_msg_id"],
                }
            )

    return {"participants": participants_order, "items": items}
