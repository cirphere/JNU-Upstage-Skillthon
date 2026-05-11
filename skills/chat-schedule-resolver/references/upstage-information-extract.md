# Upstage Information Extract (Universal)

Verified on: 2026-04-19

## What it does
Schema-driven, zero-shot extraction of named fields from any document type — invoices, receipts, bank statements, contracts, forms, and more. The caller supplies a JSON Schema describing exactly which fields to extract and what types they should be; the API returns a strictly-conforming JSON object populated with values pulled directly from the document. "Universal" means it works on arbitrary document types without per-template training or setup.

## When to choose this over related capabilities
Use Information Extract when you know exactly which fields you want (e.g., `invoice_total`, `contract_signed_date`, `line_items`) and want them returned as a structured JSON object. The other five capabilities serve different shapes of work:

- **Chat (Solar LLM)** — text generation and conversation; you would have to write extraction logic yourself and parse free-form output.
- **Document Parse** — returns the full document body with layout structure (headings, tables, paragraphs) as HTML/Markdown; pipe Parse → Information Extract when the document is huge and you want to pre-filter pages before extracting fields, or when you need both the raw structure and the extracted values.
- **Document OCR** — returns raw text plus word-level bounding boxes; no schema-driven output, no structured JSON result.
- **Document Classification** — buckets the document into a type (e.g., "invoice" vs "contract"); does not pull field values. Concrete pattern: run Classification first to determine document type, then route to a type-specific Information Extract schema.
- **Embeddings** — vectorizes text for semantic search and retrieval; does not perform extraction.

## Constraints checklist
- Max file size: 50 MB
- Max pages: 100 pages per document
- Max resolution: 200,000,000 pixels per page
- Rate limits: 1 RPS (sync) / 2 RPS (async) — see console quota page for account-level overrides
- Language / locale support: Alphanumeric, Hangul, Hanja; Hanzi/Kanji in beta
- Enhanced mode: `mode: "enhanced"` top-level parameter (Beta) for complex tables, poor scans, or handwriting — higher cost, confirm pricing against docs
- Sync schema limits: max 100 properties, max 15,000 chars total schema length
- Async schema limits: max 5,000 properties, max 120,000 chars total schema length
- Schema design restrictions: root must be `object`; first-level properties must be `string`/`integer`/`number`/`array` (`object` is NOT permitted at the first level); nested arrays not allowed (array items cannot themselves be arrays); total length of all property names and definition names ≤ 10,000 chars
- Async timeout: Not specified in docs
- Prebuilt extractors: Available for high-volume, fixed document types (fine-tuned per-type models) — same endpoint `POST /v1/information-extraction`, multipart/form-data request, `fields[]` response shape (see Caveats section)

## Supported formats
- Input: JPEG, PNG, BMP, PDF, TIFF, HEIC, DOCX, PPTX, XLSX, HWP, HWPX — submitted as a base64 data URL inside a Chat Completions `image_url` content part; plus a `response_format` JSON Schema body in the same request
- Output: JSON object whose shape strictly matches the caller-supplied schema; delivered as a stringified JSON string in `choices[0].message.content`

## Authentication
Set the environment variable `UPSTAGE_API_KEY` and pass it as a Bearer token:

```
Authorization: Bearer $UPSTAGE_API_KEY
```

When using the OpenAI SDK, supply the key as `api_key` and point `base_url` at the Information Extraction namespace:

```python
from openai import OpenAI
import os

client = OpenAI(
    api_key=os.environ["UPSTAGE_API_KEY"],
    base_url="https://api.upstage.ai/v1/information-extraction",
)
```

## API format
**Endpoint**: `POST https://api.upstage.ai/v1/information-extraction`

The API follows the OpenAI Chat Completions wire format. The document is attached as a base64 data URL inside the user message's `image_url` content part; the desired output schema is declared in `response_format`.

**Request** (schema + file):
```json
{
  "model": "information-extract",
  "messages": [
    {
      "role": "user",
      "content": [
        {
          "type": "image_url",
          "image_url": {
            "url": "data:application/octet-stream;base64,<BASE64_ENCODED_FILE>"
          }
        }
      ]
    }
  ],
  "response_format": {
    "type": "json_schema",
    "json_schema": {
      "name": "invoice_extraction",
      "schema": {
        "type": "object",
        "properties": {
          "invoice_number": {
            "type": "string",
            "description": "Unique invoice identifier printed on the document"
          },
          "invoice_date": {
            "type": "string",
            "description": "Date the invoice was issued, in the format as it appears on the document"
          },
          "due_date": {
            "type": "string",
            "description": "Payment due date as printed on the document"
          },
          "vendor_name": {
            "type": "string",
            "description": "Name of the company or individual issuing the invoice"
          },
          "buyer_name": {
            "type": "string",
            "description": "Name of the company or individual being billed"
          },
          "subtotal": {
            "type": "number",
            "description": "Sum of all line item amounts before tax, in the invoice currency"
          },
          "tax_amount": {
            "type": "number",
            "description": "Total tax charged, in the invoice currency"
          },
          "total_amount": {
            "type": "number",
            "description": "Final total including all taxes and fees, in the invoice currency"
          },
          "currency": {
            "type": "string",
            "description": "Currency code or symbol as printed on the invoice (e.g., USD, KRW, €)"
          },
          "line_items": {
            "type": "array",
            "description": "List of individual line items billed on this invoice",
            "items": {
              "type": "object",
              "properties": {
                "description": {
                  "type": "string",
                  "description": "Name or description of the product or service"
                },
                "quantity": {
                  "type": "number",
                  "description": "Number of units billed for this line item"
                },
                "unit_price": {
                  "type": "number",
                  "description": "Price per unit for this line item, in the invoice currency"
                },
                "amount": {
                  "type": "number",
                  "description": "Total amount for this line item (quantity × unit_price), in the invoice currency"
                }
              }
            }
          }
        }
      }
    }
  }
}
```

**Response**:
```json
{
  "id": "iex-AQZoWf2p5j6TO-AE",
  "choices": [
    {
      "finish_reason": "stop",
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "{\"invoice_number\":\"INV-2024-0042\",\"invoice_date\":\"2024-03-15\",\"due_date\":\"2024-04-15\",\"vendor_name\":\"Acme Supplies Co.\",\"buyer_name\":\"Globex Corporation\",\"subtotal\":1250.00,\"tax_amount\":125.00,\"total_amount\":1375.00,\"currency\":\"USD\",\"line_items\":[{\"description\":\"Widget Type A\",\"quantity\":10,\"unit_price\":75.00,\"amount\":750.00},{\"description\":\"Widget Type B\",\"quantity\":5,\"unit_price\":100.00,\"amount\":500.00}]}"
      }
    }
  ],
  "created": 1742838017,
  "model": "information-extract-260304",
  "usage": {
    "completion_tokens": 87,
    "prompt_tokens": 951,
    "total_tokens": 1038
  }
}
```

`choices[0].message.content` is a **stringified JSON string** — always call `json.loads()` on it to get the Python dict.

**Python example (full runnable)**:
```python
import base64, json, os
from openai import OpenAI

client = OpenAI(
    api_key=os.environ["UPSTAGE_API_KEY"],
    base_url="https://api.upstage.ai/v1/information-extraction",
)

with open("invoice.pdf", "rb") as f:
    b64 = base64.b64encode(f.read()).decode()

resp = client.chat.completions.create(
    model="information-extract",
    messages=[{
        "role": "user",
        "content": [{
            "type": "image_url",
            "image_url": {"url": f"data:application/octet-stream;base64,{b64}"},
        }],
    }],
    response_format={
        "type": "json_schema",
        "json_schema": {
            "name": "invoice_extraction",
            "schema": {
                "type": "object",
                "properties": {
                    "invoice_number": {"type": "string", "description": "Unique invoice identifier printed on the document"},
                    "invoice_date":   {"type": "string", "description": "Date the invoice was issued"},
                    "due_date":       {"type": "string", "description": "Payment due date"},
                    "vendor_name":    {"type": "string", "description": "Name of the issuing company or individual"},
                    "buyer_name":     {"type": "string", "description": "Name of the billed company or individual"},
                    "subtotal":       {"type": "number", "description": "Sum of line items before tax, in invoice currency"},
                    "tax_amount":     {"type": "number", "description": "Total tax charged, in invoice currency"},
                    "total_amount":   {"type": "number", "description": "Final total including tax, in invoice currency"},
                    "currency":       {"type": "string", "description": "Currency code or symbol as printed (e.g., USD, KRW)"},
                    "line_items": {
                        "type": "array",
                        "description": "Individual line items billed on this invoice",
                        "items": {
                            "type": "object",
                            "properties": {
                                "description": {"type": "string", "description": "Product or service name"},
                                "quantity":    {"type": "number", "description": "Units billed"},
                                "unit_price":  {"type": "number", "description": "Price per unit in invoice currency"},
                                "amount":      {"type": "number", "description": "Line total (quantity × unit_price)"},
                            },
                        },
                    },
                },
            },
        },
    },
)

result = json.loads(resp.choices[0].message.content)
print(result["total_amount"])
print(result["line_items"])
```

**Curl example**:
```bash
B64=$(base64 -i invoice.pdf)

curl -X POST https://api.upstage.ai/v1/information-extraction \
  -H "Authorization: Bearer $UPSTAGE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "information-extract",
    "messages": [{
      "role": "user",
      "content": [{
        "type": "image_url",
        "image_url": {"url": "data:application/octet-stream;base64,'"$B64"'"}
      }]
    }],
    "response_format": {
      "type": "json_schema",
      "json_schema": {
        "name": "invoice_extraction",
        "schema": {
          "type": "object",
          "properties": {
            "total_amount": {"type": "number", "description": "Final total including tax, in invoice currency"},
            "invoice_date": {"type": "string", "description": "Date the invoice was issued"},
            "vendor_name":  {"type": "string", "description": "Name of the company issuing the invoice"}
          }
        }
      }
    }
  }'
```

## Async API

For large documents (up to 1,000 pages) or batch jobs (up to 20 documents), use the async endpoints:

```
POST https://api.upstage.ai/v1/information-extraction/async
GET  https://api.upstage.ai/v1/information-extraction/jobs/{job_id}
```

The async request uses `multipart/form-data` with `documents` file fields and a `schema` JSON string field. Results are retained for 30 days after completion. All documents in a single job must use the same schema. HWP/HWPX are not supported in async mode. Poll every 5–10 seconds; the job reaches `COMPLETED`, `PARTIALLY_COMPLETED`, or `FAILED`.

## Parameters
| Name | Required | Default | Description |
|---|---|---|---|
| `model` | yes | — | Model alias to use. `information-extract` (points to `information-extract-260304`); `information-extract-nightly` also available |
| `messages` | yes | — | Array with a single `user` message containing an `image_url` content part whose `url` is a base64 data URL of the document |
| `response_format` | yes | — | Object with `type: "json_schema"` and a `json_schema` object containing `name` (≤ 64 chars, `[a-zA-Z0-9_-]` only) and `schema` (JSON Schema object describing the fields to extract) |
| `mode` | no | standard | (Beta) Top-level JSON body field. Set to `"enhanced"` to improve accuracy on complex tables, poor-quality scans, or handwritten content — higher cost |
| `location` | no | false | (Beta) Return bounding-box coordinates for extracted fields |
| `location_granularity` | no | element | (Beta) Coordinate detail level: `element`, `word`, or `all` |
| `confidence` | no | false | (Beta) Return `high`/`low` confidence flags per field |
| `split` | no | false | (Beta) Split multi-document files; results in separate `choices` items |
| `chunking` | no | — | (Beta) For 30+ page or 50+ row-table documents: e.g. `{"pages_per_chunk": 5}` |

## Caveats and gotchas
**Always add a `description` to every schema field.** The model uses the description to disambiguate which value in the document maps to which field. Without it, accuracy drops on documents that contain multiple similar-looking numbers or dates. Example:
```json
"total_amount": {
  "type": "number",
  "description": "final total including all taxes and fees, in the invoice currency — NOT the subtotal"
}
```

**Use `array` + item schema for repeating rows.** When a document contains line items, transaction rows, or any repeating structure, declare an `array` field with a full `items` schema — the model will populate every row. Example:
```json
"line_items": {
  "type": "array",
  "description": "all individual line items billed on this invoice",
  "items": {
    "type": "object",
    "properties": {
      "description": {"type": "string", "description": "product or service name"},
      "amount":      {"type": "number", "description": "line total in invoice currency"}
    }
  }
}
```

**`message.content` is a stringified JSON string, not a dict.** Always call `json.loads(resp.choices[0].message.content)` before accessing fields — skipping this step causes a `TypeError` or `AttributeError` at runtime.

**When `location: true` or `confidence: true`, detailed results move to `tool_calls`.** The main `choices[0].message.content` still contains the extracted field values, but coordinate and confidence data are returned in `choices[0].message.tool_calls[0].function.arguments` (function name `additional_values`). Always check for `tool_calls` before trying to read location or confidence data:
```python
result = json.loads(resp.choices[0].message.content)
if resp.choices[0].message.tool_calls:
    additional = json.loads(resp.choices[0].message.tool_calls[0].function.arguments)
    # additional contains per-field coordinates and/or confidence levels
```

**The base URL is different from Chat/Embeddings.** The namespace is `/information-extraction`, not `/v1` alone. OpenAI SDK users must set `base_url="https://api.upstage.ai/v1/information-extraction"`. Using the standard `https://api.upstage.ai/v1` base URL will return a 404 or route to the wrong handler.

**For high-volume fixed-format documents, consider Prebuilt extraction.** Upstage offers fine-tuned per-document-type models (e.g., `receipt-extraction`, `air-waybill-extraction`) that can outperform the universal model at lower cost for known schemas. The endpoint is the same `POST https://api.upstage.ai/v1/information-extraction`, but the request uses `multipart/form-data` (fields: `model` + `document` file) rather than JSON with a `response_format` schema. The response shape is also different: instead of `choices[0].message.content`, prebuilt extractors return a top-level `fields` array where each item has `key`, `type`, `value`, and `confidence` (0–1 float). Max 30 pages per document; 1 RPS per prebuilt type. Confirm pricing against docs before building a production pipeline.

**Pair with Document Parse for very large or multi-section documents.** For PDFs with 50+ pages where only a subset of pages contain the target fields, run Document Parse first to get per-element structure, identify the relevant pages, then submit only those pages to Information Extract. This avoids hitting the 100-page limit and reduces token cost.
