"""IE-mode pipeline with explicit Solar failover (C5).

Why this lives here, not inside ``extract_via_ie``:
    The C3 design split "IE-self success/failure" from "pipeline overall
    success/failure". ``extract_preferences_from_pdf`` raises on any IE
    error so callers can decide whether to retry, fall back, or fail. This
    module is the **caller** that owns the fallback decision.

Failover policy (per the C5 brief):
    * PDF synthesis failure (font missing, empty conversation, …)
        → raise immediately. No fallback — the document we'd hand IE never
          existed, so falling back to Solar would be a different feature
          path, not a "failover".
    * IE call failure OR Solar quantization adapter failure
        → fall back to the Solar-only ``step1 + step2_normalize`` path.
          Record one ``"IE_FAILOVER: <reason>"`` entry in
          ``source_notes`` so the caller can see exactly why the IE path
          was abandoned. Silent fallback is forbidden.
    * Verify (step 3) failure
        → propagate. Verify is mode-agnostic; failing it means the
          downstream contract is broken regardless of which extractor ran.

Return shape:
    {
        "backend_used":   "ie" | "solar_failover",
        "step2_result":   {participants, items},
        "step3_result":   {grounded, unresolved, verdicts, notes},
        "source_notes":   list[str]   # human-readable failover reasons
    }
"""

from __future__ import annotations

from typing import Any

from extract_via_ie import extract_preferences_from_pdf, ie_to_step2_format
from step2_normalize import normalize as step2_normalize
from step3_verify import verify_extracted_preferences
from synthesize_pdf import synthesize_pdf


def _chat_text_from_conversation(conversation: list[dict]) -> str:
    return "\n".join(f"{m['user']}: {m['text']}" for m in conversation)


def _ie_path(
    client,
    conversation: list[dict],
    reference_date: str,
    title: str,
) -> dict[str, Any]:
    """IE-only path; raises on any IE/quantization failure."""
    pdf = synthesize_pdf(conversation, title=title)
    ie_raw = extract_preferences_from_pdf(client, pdf)
    return ie_to_step2_format(client, ie_raw, reference_date=reference_date)


def _solar_path(
    client,
    conversation: list[dict],
    reference_date: str,
) -> dict[str, Any]:
    """Solar-only path; raises on any Solar Pro 3 chat failure."""
    chat_text = _chat_text_from_conversation(conversation)
    step1 = client.infer_time_preferences(chat_text, reference_date=reference_date)
    s2, _backend = step2_normalize(
        client,
        conversation_text=chat_text,
        step1_output=step1,
        reference_date=reference_date,
    )
    return s2


def run_with_failover(
    client,
    *,
    conversation: list[dict],
    reference_date: str,
    title: str = "chat",
) -> dict[str, Any]:
    """IE-first pipeline with explicit Solar fallback.

    Synthesise PDF (no fallback on failure), try IE+Solar quantization,
    and fall back to Solar step1+step2_normalize on any IE-side error.
    Verify (step 3) runs on whichever step2_result came back.

    Raises:
        Any exception from ``synthesize_pdf`` propagates unchanged — the
        PDF stage is shared infrastructure and a failure there means the
        IE-mode contract can't be honored.
        Step-3 exceptions also propagate (mode-agnostic stage).
    """
    source_notes: list[str] = []

    # PDF synthesis — shared input artifact. No fallback here per C5 policy.
    pdf = synthesize_pdf(conversation, title=title)

    # IE + Solar quantization with fallback to Solar-only step1+step2.
    backend_used = "ie"
    try:
        ie_raw = extract_preferences_from_pdf(client, pdf)
        step2_result = ie_to_step2_format(
            client, ie_raw, reference_date=reference_date,
        )
    except Exception as e:
        # Single concise note — caller can grep "IE_FAILOVER" to detect.
        reason = f"{type(e).__name__}: {str(e)[:200]}"
        source_notes.append(f"IE_FAILOVER: {reason}")
        backend_used = "solar_failover"
        step2_result = _solar_path(client, conversation, reference_date)

    chat_text = _chat_text_from_conversation(conversation)
    step3_result = verify_extracted_preferences(
        client, conversation_text=chat_text, step2_result=step2_result,
    )
    return {
        "backend_used": backend_used,
        "step2_result": step2_result,
        "step3_result": step3_result,
        "source_notes": source_notes,
    }
