"""Synthesize a Korean chat conversation into a single-page PDF table.

Purpose
-------
Upstage Information Extract (IE) accepts PDFs, images, and Office formats
but **not** plain text. To call the real IE endpoint from our chat-text
pipeline we have to render the conversation into a document IE can
accept. Of the candidates, a one-page PDF with a 4-column table
(msg_id | speaker | timestamp | text) gives IE the strongest layout
signal: speaker boundaries and message ordering are encoded by the table
grid, not just whitespace.

This module is intentionally minimal: pure ``reportlab.platypus``, no
template files, no HTML/CSS. Output is in-memory ``bytes`` by default;
pass ``save_to`` for a disk copy (debug-only).

Library choice — reportlab over weasyprint
------------------------------------------
* reportlab: pure-Python wheel, no system deps. Direct ``Table`` API for
  programmatic row-by-row construction. Font registration is one line:
  ``pdfmetrics.registerFont(TTFont(...))``. Output is deterministic.
* weasyprint: pulls Cairo/Pango/fontconfig as native system deps; better
  fit for HTML/CSS-driven layouts. Our use case generates from data, so
  the CSS layer is dead weight. The native dep chain breaks Linux CI and
  blocks ``pip install`` in fresh venvs.

reportlab also handles Korean (full Unicode) once the TTF is registered —
no Mojibake/tofu when the font has the glyphs. We register NanumGothic
explicitly so the runtime font search is deterministic across machines.

Font — NanumGothic-Regular / NanumGothic-Bold
---------------------------------------------
SIL Open Font License v1.1. Files live under ``assets/fonts/`` with the
license text in ``assets/fonts/OFL.txt``. Bold is used only for the table
header row so the column labels (msg_id/speaker/...) stand out from the
data rows.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


SKILL_ROOT = Path(__file__).resolve().parent.parent
FONT_DIR = SKILL_ROOT / "assets" / "fonts"
FONT_REGULAR = FONT_DIR / "NanumGothic-Regular.ttf"
FONT_BOLD = FONT_DIR / "NanumGothic-Bold.ttf"

_FONTS_REGISTERED = False


def _ensure_fonts_registered() -> None:
    """Register NanumGothic with reportlab. Idempotent.

    Raises:
        FileNotFoundError: if either TTF is missing. We fail fast rather
        than silently falling back to a Latin-only font that would render
        Korean as tofu blocks and silently corrupt the IE roundtrip.
    """
    global _FONTS_REGISTERED
    if _FONTS_REGISTERED:
        return
    for f in (FONT_REGULAR, FONT_BOLD):
        if not f.exists():
            raise FileNotFoundError(
                f"Required Korean font missing: {f}. Run the C1 setup "
                "to fetch NanumGothic into assets/fonts/."
            )
    pdfmetrics.registerFont(TTFont("NanumGothic", str(FONT_REGULAR)))
    pdfmetrics.registerFont(TTFont("NanumGothic-Bold", str(FONT_BOLD)))
    _FONTS_REGISTERED = True


def synthesize_pdf(
    conversation: list[dict[str, Any]],
    *,
    title: str = "단톡방 대화",
    save_to: str | Path | None = None,
) -> bytes:
    """Render a conversation as a 4-column PDF table.

    Args:
        conversation: List of ``{user, text, ts}`` dicts. ``ts`` may be
            absent or empty; the cell renders blank in that case.
        title: Document title shown at the top of the page.
        save_to: Optional path to also write the PDF to disk (debug).

    Returns:
        PDF as bytes (in-memory). When ``save_to`` is given, the same
        bytes are written there for visual inspection.

    Raises:
        FileNotFoundError: if Korean fonts aren't installed.
        ValueError: if ``conversation`` is empty (IE has nothing to find).
    """
    if not conversation:
        raise ValueError("conversation cannot be empty for PDF synthesis")
    _ensure_fonts_registered()

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=15 * mm,
        rightMargin=15 * mm,
        topMargin=15 * mm,
        bottomMargin=15 * mm,
        title=title,
        # invariant=True freezes CreationDate/ModDate/fileID so byte output
        # is deterministic — required for the C2 determinism unit test and
        # for any IE-roundtrip diffing that depends on stable input.
        invariant=True,
    )

    title_style = ParagraphStyle(
        name="title",
        fontName="NanumGothic-Bold",
        fontSize=14,
        leading=18,
        spaceAfter=8,
    )
    cell_style = ParagraphStyle(
        name="cell",
        fontName="NanumGothic",
        fontSize=10,
        leading=13,
    )

    # Build table: header row + one row per message.
    header = ["msg_id", "speaker", "timestamp", "text"]
    rows: list[list[Any]] = [header]
    for i, m in enumerate(conversation, start=1):
        rows.append(
            [
                str(i),
                Paragraph(m.get("user", ""), cell_style),
                Paragraph(m.get("ts", "") or "", cell_style),
                Paragraph(m.get("text", ""), cell_style),
            ]
        )

    # Column widths chosen so 'text' column gets the bulk of the page.
    # Total ~180mm content width on A4 (210mm − 2*15mm margins).
    col_widths = [15 * mm, 25 * mm, 25 * mm, 115 * mm]
    table = Table(rows, colWidths=col_widths, repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                # Header row
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8eef7")),
                ("FONTNAME", (0, 0), (-1, 0), "NanumGothic-Bold"),
                ("FONTSIZE", (0, 0), (-1, 0), 10),
                ("ALIGN", (0, 0), (-1, 0), "LEFT"),
                # Body rows
                ("FONTNAME", (0, 1), (-1, -1), "NanumGothic"),
                ("FONTSIZE", (0, 1), (-1, -1), 10),
                # msg_id center-aligned for visual scan
                ("ALIGN", (0, 0), (0, -1), "CENTER"),
                # Grid + padding
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#9aa3b0")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )

    story = [Paragraph(title, title_style), Spacer(1, 4), table]
    doc.build(story)
    pdf_bytes = buf.getvalue()

    if save_to:
        save_path = Path(save_to)
        save_path.parent.mkdir(parents=True, exist_ok=True)
        save_path.write_bytes(pdf_bytes)

    return pdf_bytes


# ---- PoC entry point -------------------------------------------------------

def _poc() -> Path:
    """Generate the SKILL.md 3-person golden scenario PDF for visual QA."""
    conversation = [
        {"user": "민지", "text": "이번주 늦게 보자", "ts": "오후 8:31"},
        {"user": "준호", "text": "금요일 저녁 좋아", "ts": "오후 8:33"},
        {"user": "지수", "text": "토요일 낮은 안돼", "ts": "오후 8:35"},
        {
            "user": "수아",
            "text": "다음주 점심쯤 어때, 평일 오후라면 다 괜찮음",
            "ts": "오후 8:40",
        },
        {"user": "현우", "text": "월요일이랑 화요일은 안돼", "ts": "오후 8:42"},
    ]
    out = SKILL_ROOT / "assets" / "pdf_tmp" / "poc_3person.pdf"
    synthesize_pdf(conversation, title="동기 모임 (PoC)", save_to=out)
    return out


if __name__ == "__main__":
    path = _poc()
    print(f"wrote: {path} ({path.stat().st_size} bytes)")
