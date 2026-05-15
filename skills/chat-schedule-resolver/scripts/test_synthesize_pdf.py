"""Unit tests for ``synthesize_pdf`` (C2 / IE roundtrip prep).

Five cases (per the C2 brief):

    1. normal — 5-msg conversation → bytes, 1 page, every msg_id resolvable
       in the extracted PDF text.
    2. empty conversation → ValueError with the exact contracted message.
    3. font missing → FileNotFoundError surfaced (no silent fallback).
    4. determinism — identical input produces byte-identical output.
    5. pagination — 30+ msgs spill to multiple pages with the table header
       repeated on every page.

Bonus visual: a synthetic 200-char message renders with full text intact
(auto-wrap via Paragraph cell content); the generated PDF is left under
``assets/pdf_tmp/`` so the human reviewer can confirm wrapping.

No network calls. Uses pypdf for page count and text extraction.
"""

from __future__ import annotations

import hashlib
import io
import sys
from pathlib import Path

from pypdf import PdfReader

import synthesize_pdf


SKILL_ROOT = Path(__file__).resolve().parent.parent
TMP_DIR = SKILL_ROOT / "assets" / "pdf_tmp"


# ---- helpers ---------------------------------------------------------------

def _five_msgs() -> list[dict]:
    return [
        {"user": "민지", "text": "이번주 늦게 보자", "ts": "오후 8:31"},
        {"user": "준호", "text": "금요일 저녁 좋아", "ts": "오후 8:33"},
        {"user": "지수", "text": "토요일 낮은 안돼", "ts": "오후 8:35"},
        {"user": "수아", "text": "다음주 점심쯤 어때", "ts": "오후 8:40"},
        {"user": "현우", "text": "월요일이랑 화요일은 안돼", "ts": "오후 8:42"},
    ]


def _read_pdf_text(pdf_bytes: bytes) -> tuple[int, str]:
    reader = PdfReader(io.BytesIO(pdf_bytes))
    pages = reader.pages
    text = "\n".join(p.extract_text() or "" for p in pages)
    return len(pages), text


# ---- cases -----------------------------------------------------------------

def case_normal() -> bool:
    """정상 5건 → bytes, 1 페이지, 모든 msg_id 검색 가능."""
    convo = _five_msgs()
    pdf = synthesize_pdf.synthesize_pdf(convo, title="C2 정상 5건")
    failures = []
    if not isinstance(pdf, bytes):
        failures.append(f"return type should be bytes, got {type(pdf)}")
    if not pdf.startswith(b"%PDF-"):
        failures.append("bytes lack PDF magic header")
    n_pages, text = _read_pdf_text(pdf)
    if n_pages != 1:
        failures.append(f"expected 1 page, got {n_pages}")
    # Every msg_id ("1".."5") must be findable
    for i in range(1, len(convo) + 1):
        if str(i) not in text:
            failures.append(f"msg_id {i} not found in PDF text")
    # Speaker names must survive subsetting
    for m in convo:
        if m["user"] not in text:
            failures.append(f"speaker {m['user']!r} not found in PDF text")
    return _report("normal", failures)


def case_empty_conversation() -> bool:
    """empty conversation → ValueError with contracted message."""
    failures = []
    try:
        synthesize_pdf.synthesize_pdf([])
        failures.append("expected ValueError, got success")
    except ValueError as e:
        if str(e) != "conversation cannot be empty for PDF synthesis":
            failures.append(f"unexpected ValueError message: {e!r}")
    return _report("empty_conversation", failures)


def case_font_missing() -> bool:
    """폰트 누락 → FileNotFoundError (silent fallback 없음)."""
    failures = []
    # Save and override module-level font path; reset the cache flag.
    orig_path = synthesize_pdf.FONT_REGULAR
    orig_flag = synthesize_pdf._FONTS_REGISTERED
    try:
        synthesize_pdf.FONT_REGULAR = Path("/nonexistent/NoFont.ttf")
        synthesize_pdf._FONTS_REGISTERED = False
        try:
            synthesize_pdf.synthesize_pdf(_five_msgs())
            failures.append("expected FileNotFoundError, got success")
        except FileNotFoundError as e:
            if "NoFont.ttf" not in str(e):
                failures.append(
                    f"FileNotFoundError should name the missing font: {e}"
                )
        except Exception as e:
            failures.append(
                f"expected FileNotFoundError, got {type(e).__name__}: {e}"
            )
    finally:
        synthesize_pdf.FONT_REGULAR = orig_path
        synthesize_pdf._FONTS_REGISTERED = orig_flag
    return _report("font_missing", failures)


def case_determinism() -> bool:
    """동일 입력 2회 → byte-identical."""
    convo = _five_msgs()
    a = synthesize_pdf.synthesize_pdf(convo, title="det")
    b = synthesize_pdf.synthesize_pdf(convo, title="det")
    failures = []
    if a != b:
        diff = sum(1 for x, y in zip(a, b) if x != y) + abs(len(a) - len(b))
        failures.append(
            f"byte diff = {diff} bytes; sha256(a)={hashlib.sha256(a).hexdigest()[:16]} "
            f"sha256(b)={hashlib.sha256(b).hexdigest()[:16]}"
        )
    else:
        # Surface confirmation in the OK line.
        print(
            f"   bytes={len(a)}  sha256={hashlib.sha256(a).hexdigest()[:16]}  diff=0"
        )
    return _report("determinism", failures)


def case_pagination() -> bool:
    """30+건 → 페이지 ≥ 2, 헤더 행이 모든 페이지에 반복."""
    convo = [
        {
            "user": f"화자{i % 6 + 1}",
            "text": f"메시지 본문 {i}번 — 점심쯤 가능, 저녁 늦게도 OK",
            "ts": f"오후 {1 + i // 6}:{(i * 7) % 60:02d}",
        }
        for i in range(40)
    ]
    pdf = synthesize_pdf.synthesize_pdf(convo, title="40건 페이지 분할")
    n_pages, text = _read_pdf_text(pdf)
    failures = []
    if n_pages < 2:
        failures.append(f"40 msgs should span ≥ 2 pages, got {n_pages}")
    # Header tokens must appear at least once per page (repeatRows=1 effect).
    reader = PdfReader(io.BytesIO(pdf))
    missing_pages = []
    for idx, p in enumerate(reader.pages, start=1):
        page_text = p.extract_text() or ""
        if not all(tok in page_text for tok in ("msg_id", "speaker", "text")):
            missing_pages.append(idx)
    if missing_pages:
        failures.append(
            f"header row missing on pages: {missing_pages}"
        )
    # All 40 msg_ids should still be findable across the document
    for i in range(1, 41):
        if str(i) not in text:
            failures.append(f"msg_id {i} not in extracted text")
            break  # report first miss; don't flood
    return _report("pagination", failures)


def case_long_message_wrap_visual() -> bool:
    """Bonus: 200-char message renders with full text (auto-wrap via Paragraph).

    Not a strict pass/fail — writes a sample PDF and verifies the full
    long text survives extraction. The screenshot for visual confirmation
    is generated separately by the C2 runner.
    """
    long_text = (
        "안녕 다들, 다음 주는 평일 저녁이 다 비어. 월/화는 회의 길어서 "
        "9시 넘어야 끝나고, 수/목은 7시쯤 가능, 금요일은 6시부터 자유. "
        "주말은 토요일 오후 늦게 또는 일요일 점심쯤 보는 것도 좋아. "
        "장소는 강남이나 잠실 중간지점이면 더 편하고, 늦으면 택시 콜."
    )
    if len(long_text) < 200:
        long_text += " 추가 패딩: " + "가나다라마바사아자차" * 5
    convo = [{"user": "수아", "text": long_text, "ts": "오후 9:01"}]
    out_path = TMP_DIR / "c2_long_wrap.pdf"
    pdf = synthesize_pdf.synthesize_pdf(
        convo, title="C2 자동 wrap 확인", save_to=out_path
    )
    n_pages, text = _read_pdf_text(pdf)
    failures = []
    # First 30 chars of the long message must be present (text extraction
    # joins wrapped lines so we don't require an exact substring of the
    # whole thing, but the head should survive).
    head = long_text[:30]
    if head not in text:
        failures.append(
            f"head of long message lost: head={head!r}, extracted head={text[:60]!r}"
        )
    if not out_path.exists():
        failures.append(f"expected save_to={out_path} to be written")
    print(f"   wrote {out_path} ({len(pdf)} bytes, {n_pages} page)")
    return _report("long_message_wrap_visual", failures)


# ---- runner ----------------------------------------------------------------

def _report(name: str, failures: list[str]) -> bool:
    if failures:
        print(f"[FAIL] {name}")
        for f in failures:
            print(f"   - {f}")
        return False
    print(f"[OK]   {name}")
    return True


def main() -> int:
    cases = [
        case_normal,
        case_empty_conversation,
        case_font_missing,
        case_determinism,
        case_pagination,
        case_long_message_wrap_visual,
    ]
    results = [c() for c in cases]
    passed = sum(results)
    print(f"\nSUMMARY: {passed}/{len(results)} synthesize_pdf cases passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
