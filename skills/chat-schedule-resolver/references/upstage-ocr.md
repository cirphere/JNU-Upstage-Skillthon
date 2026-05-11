# Upstage Document OCR

Verified on: 2026-04-19

## What it does
Extracts plain text with word-level bounding boxes and confidence scores from scanned images, photos, and multi-page documents — turning pixels into searchable, positionable text. Does NOT produce document structure (headings, tables, figures); for that, use Document Parse.

## When to choose this over related capabilities
- **Chat (Solar LLM)** → text generation and reasoning; not a perception/extraction tool.
- **Document Parse** → layout-aware extraction that preserves structure (headings, tables, figures, markdown/HTML output); use Parse for born-digital PDFs or when you need the document tree. If you need structure AND the source is a scan, use Document Parse with `ocr=force` — that wraps OCR + layout detection in one call, so there is no need to stitch OCR results yourself.
- **Information Extract (Universal)** → schema-driven named-field extraction; accepts OCR'd text or a raw file as input.
- **Document Classification** → buckets a document into a category; does not return text content.
- **Embeddings** → vectorizes text for semantic search; does not extract text from images.

**Concrete decision rule:** Use OCR when the input is a phone photo, a flatbed scan, or any rasterized image and you only need the raw text plus per-word pixel coordinates. If you also need to reason about the document's layout (headings, tables, multi-column flow), use Document Parse instead.

## Constraints checklist
- Max file size: 50 MB
- Max pages: 100 pages per file (sync only — no async endpoint for OCR)
- Max pixels per page: 200,000,000 (e.g., ~14,142 × 14,142 px)
- Optimal text size: text occupying ≤ ~30% of the page area; larger text may cause recognition errors
- Rate limits / quota: **1 RPS** and **300 PPM** (Tier 0 defaults; higher tiers raise these limits)
- Language / locale support:
  - Full support: Alphanumeric (Latin), Hangul (Korean), Hanja (Korean-Chinese characters)
  - Partial support: Katakana, Hiragana (Japanese)
  - Beta support: Hanzi (Simplified Chinese), Kanji (Japanese)
- Bounding box coordinates: absolute pixel values (not normalized 0–1)
- No async endpoint — all calls are synchronous; plan accordingly for 100-page PDFs

## Supported formats
- Input: JPEG, PNG, BMP, PDF, TIFF, HEIC, DOCX, PPTX, XLSX, HWP, HWPX
- Output: JSON with the following top-level shape:
  - `text` — full document text (all pages concatenated)
  - `pages[]` — per-page objects each containing `text`, `width`, `height`, `confidence`, and `words[]`
  - `pages[].words[]` — per-word objects with `text`, `confidence`, and `boundingBox.vertices` (four `{x, y}` corner points in absolute pixels)
  - `confidence` — document-level confidence (0–1)
  - `numBilledPages` — billable page count
  - `modelVersion` — version string of the model that ran (e.g., `ocr-250904`)
  - `metadata.pages[]` — page dimensions (`width`, `height`, `page` index)

## Authentication
Set the environment variable `UPSTAGE_API_KEY` to your key from console.upstage.ai/api-keys. Pass it as a Bearer token:

```bash
Authorization: Bearer $UPSTAGE_API_KEY
```

## API format
**Endpoint**: `POST https://api.upstage.ai/v1/document-digitization`

OCR and Document Parse share this endpoint. The `model` form field selects which capability runs. There is no separate OCR-only URL.

**Request** (multipart/form-data):
```bash
# multipart form — three fields at most
model=ocr            # required; alias resolves to ocr-250904
document=<file>      # required; binary file part
schema=<value>       # optional; only needed when migrating from Clova OCR or Google Vision
```

**Response**:
```json
{
  "apiVersion": "1.1",
  "confidence": 0.9924988460974842,
  "mimeType": "multipart/form-data",
  "modelVersion": "ocr-250904",
  "numBilledPages": 1,
  "stored": true,
  "metadata": {
    "pages": [
      {"height": 1600, "page": 1, "width": 1200}
    ]
  },
  "text": "Invoice\nDate: 2026-01-15\nTotal: $142.00",
  "pages": [
    {
      "id": 0,
      "confidence": 0.97,
      "width": 1200,
      "height": 1600,
      "text": "Invoice\nDate: 2026-01-15\nTotal: $142.00",
      "words": [
        {
          "id": 0,
          "text": "Invoice",
          "confidence": 0.9950619419121907,
          "boundingBox": {
            "vertices": [
              {"x": 50,  "y": 75},
              {"x": 150, "y": 75},
              {"x": 150, "y": 100},
              {"x": 50,  "y": 100}
            ]
          }
        }
      ]
    }
  ]
}
```

**Curl example**:
```bash
curl -X POST https://api.upstage.ai/v1/document-digitization \
  -H "Authorization: Bearer $UPSTAGE_API_KEY" \
  -F "model=ocr" \
  -F "document=@/path/to/scan.pdf"
```

**Python example**:
```python
import os
import requests

def ocr_document(file_path: str) -> dict:
    with open(file_path, "rb") as f:
        response = requests.post(
            "https://api.upstage.ai/v1/document-digitization",
            headers={"Authorization": f"Bearer {os.environ['UPSTAGE_API_KEY']}"},
            files={"document": f},
            data={"model": "ocr"},
        )
    response.raise_for_status()
    return response.json()

result = ocr_document("scan.pdf")
print(result["text"])                              # full text
for word in result["pages"][0]["words"]:
    print(word["text"], word["boundingBox"]["vertices"])
```

## Parameters
| Name | Required | Default | Description |
|---|---|---|---|
| `document` | yes | — | Binary file part; any supported format up to 50 MB |
| `model` | yes | — | Must be `ocr` (alias for `ocr-250904`); determines OCR capability vs. Document Parse on the shared endpoint |
| `schema` | no | native Upstage format | Response schema compatibility shim: `clova` reshapes output to match Clova OCR response format; `google` reshapes to match Google Vision API format. Omit for new integrations. |

## Caveats and gotchas
- **Shared endpoint, mandatory `model` field**: Because `POST /v1/document-digitization` also serves Document Parse, always include `model=ocr` in the form data. Omitting it or passing `model=document-parse` will return structured HTML/Markdown output instead of word-level OCR.
- **No async support**: OCR is synchronous only (max 100 pages). When the source document exceeds 100 pages, split it before submitting. For large-scale batch processing consider Document Parse async instead.
- **Bounding boxes are absolute pixels**: Coordinates in `words[].boundingBox.vertices` are raw pixel offsets relative to the page raster (top-left origin). Scale or normalize them yourself if downstream tools expect ratios.
- **Large-text degradation**: Text that occupies more than ~30% of the page area (e.g., poster-size headlines or cover-page titles in low-resolution scans) may produce recognition errors. Pre-process such pages by cropping or downscaling the text region before sending.
- **Need structure too?**: When you also need headings, tables, or multi-column layout from a scanned document, call Document Parse with `model=document-parse` and `ocr=force` rather than calling OCR first and then stitching results — Parse wraps OCR plus layout detection in a single call.
- **Korean handling**: Hangul and Hanja have full support. Japanese kana are partially supported. Simplified Chinese Hanzi and Japanese Kanji are in beta — verify results on a sample before deploying to production.
- **Migration from other OCR providers**: If replacing Clova OCR or Google Vision in an existing pipeline, pass `schema=clova` or `schema=google` to get a response envelope that matches the old format, reducing downstream code changes.
- **`stored` field**: When `stored: true` in the response, the input file was retained on Upstage servers per their data-retention policy. Review the policy in the console if data residency matters for your use case.
