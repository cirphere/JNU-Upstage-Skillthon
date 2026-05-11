---
name: chat-schedule-resolver
description: Resolve a group meeting time from a Korean group-chat conversation (text or KakaoTalk screenshot) plus participants' calendar free-time. Returns Top-N candidate slots with Korean-language rationales tracing back to specific utterances and constraints. Use this skill whenever the user shares a 단톡방/group chat and asks "시간 잡아줘", "약속 시간 추천", "회의 조율", "이 캡처 보고 시간 정해줘" — even when they don't say the word "skill". Single-purpose; the skill itself does not parse ICS or send invites — callers (Agents) feed it free-time JSON.
---

# chat-schedule-resolver

## What this does

Given (1) a Korean group-chat conversation and (2) each participant's
free-time windows, return Top-N candidate meeting slots, each with a
Korean-language rationale that cites specific utterances and constraints.

Single-purpose by design: the skill does **not** parse ICS, query calendars,
or send invites. A caller Agent does that and hands the skill plain JSON.
This keeps the skill reusable across Slack / Discord / KakaoTalk surfaces.

## When to use

Trigger this skill when the user shares group-chat content (text paste or
KakaoTalk screenshot) and asks for a meeting time. Example phrasings:

- "이 단톡 보고 시간 잡아줘"
- "다같이 가능한 시간 뽑아줘"
- "이 캡처에서 약속 시간 추천"
- "회의 시간 조율해줘"

Do **not** use this skill for: 1-on-1 scheduling, calendar parsing,
sending invites, or non-Korean conversations.

## Input

```json
{
  "conversation": {
    "text": "민지: 이번주 늦게 보자\n준호: 금요일 저녁 좋아\n지수: 토요일 낮은 안돼",
    "image_path": null
  },
  "calendars": {
    "민지": [{"start": "2026-05-15T18:00", "end": "2026-05-15T22:00"}],
    "준호": [{"start": "2026-05-15T19:00", "end": "2026-05-15T23:00"}],
    "지수": [{"start": "2026-05-15T19:00", "end": "2026-05-15T21:00"}]
  },
  "reference_date": "2026-05-11",
  "top_n": 3
}
```

- Exactly one of `conversation.text` or `conversation.image_path` is required.
  If both are present, OCR runs on the image and is concatenated with the text.
- `calendars` keys must match the participant names that appear in the
  conversation. The skill does not infer aliases.
- `reference_date` anchors relative phrases like "이번주", "다음주", "내일".

## Output

```json
{
  "recommended_slots": [
    {
      "start": "2026-05-15T19:00",
      "end": "2026-05-15T21:00",
      "confidence": 0.92,
      "participants_available": ["민지", "준호", "지수"],
      "type": "everyone",
      "rationale": "민지의 '이번주 늦게', 준호의 '금요일 저녁'과 일치하며 지수의 '토요일 낮은 안돼' 제약을 위반하지 않음. 세 명 모두 캘린더 비어있음."
    }
  ],
  "extraction_trace": {
    "items": [
      {"participant": "민지", "polarity": "prefer",  "time_expr_raw": "이번주 늦게",     "start": "2026-05-15T20:00", "end": "2026-05-16T00:00", "certainty": 0.85, "source_msg_id": 1, "gc_verdict": "grounded"},
      {"participant": "지수", "polarity": "exclude", "time_expr_raw": "토요일 낮은 안돼", "start": "2026-05-16T11:00", "end": "2026-05-16T16:00", "certainty": 0.80, "source_msg_id": 3, "gc_verdict": "grounded"}
    ],
    "unresolved": []
  }
}
```

Field language contract: `rationale` is Korean natural language. All other
keys and string values are English/ISO so caller Agents can parse them.

## Pipeline

```
[input]
  image?  -> OCR (model=ocr)               -> raw text
            + Solar Pro 3 reformat         -> "speaker: msg" lines
  text   -----------------------------------+
                                            v
  chat text  ->  Solar Pro 3                -> free-form per-speaker
                 (model=solar-pro3)            time prefs/constraints
                                            v
  + chat text  ->  Solar Pro 3 structured   -> strict-schema items
                   (model=solar-pro3,          {participant, polarity,
                    response_format=             time_expr_raw, start,
                    json_schema)                 end, certainty,
                                                 source_msg_id}
                                            v
  per (participant, phrase)  ->  Solar      -> grounded / unresolved
                                 Pro 3 judge   partition
                                 (LLM-as-
                                  judge)
                                            v
  grounded windows intersect calendars[]    -> Top-N candidates + Korean
                                               rationale (caller-side)
```

Each step's request shape lives in the local references — read the one you
need rather than guessing:

- `references/upstage-ocr.md` — OCR endpoint, multipart form, `model=ocr`.
- `references/upstage-chat.md` — Solar Pro 3 chat completions. The
  workhorse for v1: used for first-pass inference, strict-schema
  extraction (`response_format=json_schema`), groundedness judging, and
  OCR reformatting.
- `references/upstage-information-extract.md` — kept for reference but
  **not used in v1**. The IE endpoint accepts documents (base64 image/PDF),
  not plain text; passing chat text returns HTTP 400 (`data:text/plain`
  is not in the supported-formats list). v1 routes extraction through
  Solar Pro 3 + `response_format=json_schema` instead.
- Groundedness Check has **no live endpoint** as of 2026-05 (every
  candidate model name — `solar-1-mini-groundedness-check`,
  `groundedness-check`, `solar-groundedness-check` — returns
  `400 invalid_request_body: model is invalid or no longer supported`;
  `langchain_upstage 0.7.7` removed the `UpstageGroundednessCheck` class
  for the same reason). v1 implements the gate via Solar Pro 3 as an
  LLM-judge with strict JSON output (`{verdict: grounded|not_grounded|
  unsure, score, evidence_msg_id, reason}`).

## Extraction schema (Solar Pro 3 structured output)

Pass this as `response_format.json_schema.schema` on a Solar Pro 3 chat
completion. `strict: true` and `additionalProperties: false` are required at
every object level; every property listed in `required`.

The schema deliberately avoids nested arrays (an Upstage structured-output
constraint also shared with IE) by flattening multi-day phrases into one
row per (day × time-window). `"다음주 점심쯤"` → 7 rows, each with a single
contiguous interval; `"주말"` → 2 rows (Sat + Sun).

```json
{
  "type": "object",
  "properties": {
    "participants": {"type": "array", "items": {"type": "string"}},
    "items": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "participant":    {"type": "string"},
          "polarity":       {"type": "string", "enum": ["prefer", "exclude"]},
          "time_expr_raw":  {"type": "string"},
          "start":          {"type": "string", "description": "ISO 8601 local, inclusive"},
          "end":            {"type": "string", "description": "ISO 8601 local, exclusive; use next-day 00:00 not '24:00'"},
          "certainty":      {"type": "number"},
          "source_msg_id":  {"type": "integer", "description": "1-based line in conversation"}
        },
        "required": ["participant", "polarity", "time_expr_raw", "start", "end", "certainty", "source_msg_id"],
        "additionalProperties": false
      }
    }
  },
  "required": ["participants", "items"],
  "additionalProperties": false
}
```

## Groundedness gate

Solar Pro 3 acts as an LLM-judge: it sees the full numbered chat and one
claim of the form `"{participant}는(은) '{time_expr_raw}'을(를)
{선호한다|배제한다}."`, then returns
`{verdict: grounded|not_grounded|unsure, score, evidence_msg_id, reason}`
via `response_format=json_schema`. Drop rows whose verdict is not
`"grounded"` (default; `unsure` is treated conservatively as a drop).

This is the hallucination guardrail — without it the extractor occasionally
invents preferences for participants who never spoke about timing. Judge per
unique `(participant, time_expr_raw)` pair, not per row, since one phrase
expands into many multi-day rows and the verdict only depends on the source
phrase.

## Ranking

After intersecting grounded windows with the per-participant `calendars[]`
free-time, rank candidates by:

1. `participants_available` count (descending) — prefer slots everyone can
   attend over partial matches.
2. Sum of soft-preference matches (descending).
3. Slot duration (descending) — longer windows beat tighter ones.
4. Earliness (ascending) — break ties by sooner-is-better.

Emit Top-N. If no slot satisfies everyone, return the best partial matches
and set `type: "partial"` with `participants_available` listing who fits.

## Failure modes to handle

- **Empty conversation after OCR**: return `recommended_slots: []` with an
  `error` field explaining the OCR yielded no usable text.
- **Participant name mismatch** between conversation and `calendars` keys:
  surface the mismatch in `extraction_trace` rather than silently dropping.
- **No overlap at all**: return `type: "partial"` candidates, do not throw.
- **`UPSTAGE_API_KEY` missing**: fail fast at startup (handled by
  `scripts/upstage_client.py`).

## v2 roadmap (deferred)

Embeddings-based per-user response pattern learning lives under
`embeddings/` (currently empty). See `embeddings/README.md` for the plan.
Not wired into v1 to keep the MVP shippable by 2026-05-27.

## Project layout

```
chat-schedule-resolver/
├── SKILL.md
├── assets/
│   ├── .env.example       # template; copy to .env and fill UPSTAGE_API_KEY
│   └── .env               # (gitignored — user-created)
├── scripts/
│   └── upstage_client.py  # thin wrapper over the 4 v1 endpoints
├── references/
│   ├── upstage-ocr.md
│   ├── upstage-information-extract.md
│   └── upstage-chat.md
└── embeddings/            # v2 placeholder; do not use in v1
    └── README.md
```
