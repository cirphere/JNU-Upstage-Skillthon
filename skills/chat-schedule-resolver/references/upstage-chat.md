# Upstage Chat (Solar LLM)

Verified on: 2026-04-19

## What it does

Generates, transforms, and reasons over text using Upstage's Solar LLM family — covering chat, summarization, translation, function/tool calling, structured JSON output, and Korean-dominant multilingual reasoning. It exposes an OpenAI-compatible chat completions endpoint, so any code already using the OpenAI SDK can switch to Solar with a two-line change (`base_url` and `model`).

## When to choose this over related capabilities

| Peer capability | Use that instead when… | Use Chat instead when… |
|---|---|---|
| **Document Parse** | You need layout-aware extraction of headings, tables, and reading order from a PDF or scanned image — the output is structured HTML/Markdown. | You already have plain text and need to generate, summarize, or reason over it. |
| **Document OCR** | You need raw text plus word-level bounding boxes from a scanned image or handwriting — no generation, just perception. | You have text already extracted and want the model to do something with it. |
| **Information Extract (Universal)** | You need to pull specific named fields (e.g. `invoice_total`, `vendor_name`) from a document against a caller-defined JSON Schema. | The output is open-ended generation, a conversation, or reasoning — not slot-filling. |
| **Document Classification** | You need to bucket a document into one of your predefined categories (invoice / contract / receipt / other). | You want the model to produce text, not a label. |
| **Embeddings** | You need a dense vector representation of text for semantic search, similarity scoring, or RAG retrieval. | You want the model to read, reason, and write — not encode. |

The common pipeline for RAG over PDFs is: Document Parse → Embeddings (index time) → Chat (answer time). Chat is the generation step; it does not perceive documents or images directly.

## Constraints checklist

- **Max file size / pages / DPI:** N/A — Chat accepts text only; no file or image upload in this endpoint. For document input, parse first and pass the extracted text in `messages`.
- **Max tokens (input + output):**
  - `solar-pro3`: 128K context window total (input + output combined)
  - `solar-pro2`: 65K context window total
  - `solar-mini`: 32K context window total
  - `syn-pro`: Not specified in docs
  - `max_tokens` caps output length; if omitted the model uses its internal default
- **Rate limits / quota (Tier 0):** 100 RPM, 50,000 TPM — applies to all models; see console quota page for higher tiers
- **Language / locale support:** Korean and English are first-class; Japanese supported; general multilingual capability. Pin the system prompt to your dominant locale for consistent output language.
- **Tool-call count:** Maximum 128 function definitions per request
- **Structured output schema:** Max nesting 3 levels; `strict: true` and `additionalProperties: false` required; all object properties must be in `required`; no recursive `$ref`
- **`parallel_tool_calls`:** Available on `solar-pro3` only; ignored on other models
- **`syn-pro` model:** Optimized for synthetic data generation; function calling and `reasoning_effort` are not supported

## Supported formats

- **Input:** `messages` array (OpenAI-style roles: `system`, `user`, `assistant`, `tool`); plain text content strings or content arrays. No `image_url` or file/audio upload on this endpoint — extract text first using Document Parse or OCR, then pass it as message content.
- **Output:** `assistant` message with `content` string; optional `reasoning` field when `reasoning_effort` is `medium` or higher; `tool_calls` array for function calling; streaming chunks via server-sent events when `stream: true`; guaranteed-valid JSON object when `response_format` specifies a JSON schema.

## Authentication

```bash
export UPSTAGE_API_KEY="your-key-from-console"
# Header format:
Authorization: Bearer $UPSTAGE_API_KEY
```

## API format

**Endpoint**: `POST https://api.upstage.ai/v1/chat/completions`

**Request**:
```json
{
  "model": "solar-pro3",
  "messages": [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "Summarize the key points of the Paris Agreement."}
  ],
  "max_tokens": 512,
  "temperature": 0.7,
  "stream": false
}
```

**Response**:
```json
{
  "id": "chatcmpl-abc123",
  "object": "chat.completion",
  "created": 1723911735,
  "model": "solar-pro3",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "The Paris Agreement establishes three main goals: ...",
        "reasoning": null
      },
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 42,
    "completion_tokens": 128,
    "total_tokens": 170,
    "completion_tokens_details": {
      "reasoning_tokens": 0
    }
  }
}
```

**Curl example**:
```bash
curl -X POST https://api.upstage.ai/v1/chat/completions \
  -H "Authorization: Bearer $UPSTAGE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "solar-pro3",
    "messages": [
      {"role": "system", "content": "You are a helpful assistant."},
      {"role": "user", "content": "Summarize the key points of the Paris Agreement."}
    ],
    "max_tokens": 512,
    "temperature": 0.7
  }'
```

**OpenAI SDK (Python) — drop-in swap**:
```python
import os
from openai import OpenAI

client = OpenAI(
    api_key=os.environ["UPSTAGE_API_KEY"],
    base_url="https://api.upstage.ai/v1",
)

response = client.chat.completions.create(
    model="solar-pro3",
    messages=[
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Summarize the key points of the Paris Agreement."},
    ],
    max_tokens=512,
)
print(response.choices[0].message.content)
```

**Streaming**:
```python
stream = client.chat.completions.create(
    model="solar-pro3",
    messages=[{"role": "user", "content": "Tell me a story"}],
    stream=True,
)
for chunk in stream:
    if chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="")
```

**Function calling (two-turn pattern)**:
```python
tools = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get current temperature for a city",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "City name"}
                },
                "required": ["city"],
            },
        },
    }
]

# Turn 1: model returns tool_calls
messages = [{"role": "user", "content": "What is the weather in Seoul?"}]
resp = client.chat.completions.create(
    model="solar-pro3", messages=messages, tools=tools, tool_choice="auto"
)
tool_call = resp.choices[0].message.tool_calls[0]
# tool_call.function.name -> "get_weather"
# tool_call.function.arguments -> '{"city": "Seoul"}'

# Turn 2: submit the tool result
messages.append(resp.choices[0].message)           # assistant turn (preserves tool_calls)
messages.append({
    "role": "tool",
    "tool_call_id": tool_call.id,
    "name": tool_call.function.name,
    "content": '{"temperature": 15, "unit": "celsius"}',
})
final = client.chat.completions.create(model="solar-pro3", messages=messages, tools=tools)
print(final.choices[0].message.content)
```

**Structured output (JSON schema)**:
```python
response = client.chat.completions.create(
    model="solar-pro3",
    messages=[{"role": "user", "content": "Extract: name and age from 'Alice is 30 years old.'"}],
    response_format={
        "type": "json_schema",
        "json_schema": {
            "name": "person",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "age":  {"type": "integer"},
                },
                "required": ["name", "age"],
                "additionalProperties": False,
            },
        },
    },
)
import json
data = json.loads(response.choices[0].message.content)
# data -> {"name": "Alice", "age": 30}
```

**Reasoning effort (solar-pro3)**:
```python
response = client.chat.completions.create(
    model="solar-pro3",
    messages=[{"role": "user", "content": "Solve: if 3x + 7 = 22, what is x?"}],
    reasoning_effort="high",   # "minimal" | "low" | "medium" | "high"
)
# response.choices[0].message.reasoning  -> chain-of-thought (string)
# response.choices[0].message.content    -> final answer
```

## Parameters

| Name | Required | Default | Description |
|---|---|---|---|
| `model` | yes | — | `solar-pro3` (128K, 102B MoE), `solar-pro2` (65K, 31B), `solar-mini` (32K, 10.7B), `syn-pro` (synthetic data; no tools/reasoning) |
| `messages` | yes | — | Array of `{role, content}` objects. Roles: `system`, `user`, `assistant`, `tool` |
| `max_tokens` | no | model default | Maximum tokens to generate in the response |
| `temperature` | no | 0.8 (pro3), 0.7 (others) | Sampling randomness, range 0–2. Lower = more deterministic |
| `top_p` | no | 0.95 (pro3), 1.0 (others) | Nucleus sampling threshold, range 0–1 |
| `stream` | no | `false` | Return server-sent event chunks instead of a full response |
| `frequency_penalty` | no | `1.1` | Penalize tokens by existing frequency in output, range −2 to 2 |
| `presence_penalty` | no | `0.0` | Penalize tokens that have appeared at all, range −2 to 2 |
| `stop` | no | — | Array of stop strings; generation halts when any is produced |
| `reasoning_effort` | no | `"minimal"` | `"minimal"` / `"low"` / `"medium"` / `"high"`. Controls reasoning token budget. pro3: `low`/`minimal` = reasoning OFF (fastest); medium = reasoning ON, up to 30% of remaining context (min 4K, max 16K tokens); high = reasoning ON, up to 60% (min 8K, max 32K). pro2: medium/high enable reasoning; low/minimal disable it. solar-mini/syn-pro: ignored. |
| `tools` | no | — | Array of function definitions (max 128). Each: `{"type":"function","function":{"name","description","parameters"}}` |
| `tool_choice` | no | `"auto"` | `"none"`, `"auto"`, `"required"`, or `{"type":"function","function":{"name":"fn_name"}}` |
| `parallel_tool_calls` | no | `true` | Allow multiple simultaneous tool calls per response. solar-pro3 only. |
| `response_format` | no | — | `{"type":"json_schema","json_schema":{...}}` for guaranteed-valid structured JSON |
| `prompt_cache_key` | no | — | Session identifier string; hints the backend to reuse cached KV state for a repeated prefix (e.g. long system prompt) |

## Caveats and gotchas

**Use the short alias, not the versioned model ID.** The alias `solar-pro3` always resolves to the current stable checkpoint. Pinning to a versioned ID locks you to a specific version that may be deprecated without notice. Example: `"model": "solar-pro3"`. Current checkpoints (as of the last spec update): `solar-pro3` → `solar-pro3-260323`, `solar-pro2` → `solar-pro2-251215`, `solar-mini` → `solar-mini-250422`, `syn-pro` → `syn-pro-251021`.

**Function calling requires the two-turn pattern.** When the model returns `finish_reason: "tool_calls"`, append the assistant message (with `tool_calls` intact) and a `tool` role message with `tool_call_id`, `name`, and `content` (the function's result as a JSON string), then call the API again to get the final answer. Skipping the second call leaves the conversation incomplete.

**Function names must match `[a-zA-Z0-9_-]` and be ≤ 64 characters.** A name like `"get-weather_v2"` is valid; `"get weather!"` or a 65-character string will cause a validation error.

**Structured output schemas require `strict: true` and `additionalProperties: false` at every object level, with all properties listed in `required`.** A schema valid under OpenAI's validator may still be rejected if these constraints are missing. Nesting beyond 3 levels and recursive `$ref` definitions are not supported.

**`reasoning_effort` costs tokens and time.** For straightforward tasks (translation, simple Q&A), keep the default `"minimal"` to minimize latency and cost. Raise to `"medium"` or `"high"` for multi-step math, complex code generation, or logical reasoning. Reasoning tokens are included in `completion_tokens` and billed. When reasoning is enabled, inspect `message.reasoning` for the chain-of-thought and `completion_tokens_details.reasoning_tokens` for the token count.

**Korean/English code-switching is handled natively, but pin the output language explicitly.** Without a clear system prompt language directive, the model may switch languages mid-response. Example system prompt: `"You are a helpful assistant. Always reply in Korean."` — this keeps output consistent when the user might write in either language.

**`prompt_cache_key` speeds up repeated calls with a long shared prefix.** When your system prompt is static and large (e.g. a 10K-token knowledge base injected every turn), setting the same `prompt_cache_key` string across requests in a session lets the backend reuse cached key-value state, reducing both latency and effective token cost.

**`syn-pro` is for synthetic data generation only.** It does not support `tools`, `tool_choice`, or `reasoning_effort`. Use `solar-pro3` or `solar-pro2` for agentic or reasoning workflows.

**On 429 Too Many Requests, back off and retry.** Rate limits are per-minute (100 RPM / 50K TPM at Tier 0). Implement exponential backoff starting at ~1 second; the limit resets each minute.
