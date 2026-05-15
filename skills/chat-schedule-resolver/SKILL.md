---
name: chat-schedule-resolver
description: 단톡방의 자연어 대화와 참여자 캘린더를 입력받아, 모두가 가능한 시간 후보 Top-N을 근거와 함께 출력하는 단일 목적 Skill. 단톡방 캡처(.png) 또는 대화 텍스트(JSON) + 참여자 캘린더(빈 시간)를 받아 Solar Pro 3로 한국어 시간 표현을 정량화하고, 30분 슬롯 교집합·랭킹으로 Top-N 후보를 반환한다. 사용 시점은 사용자가 단체 일정 조율 중이거나, Agent가 대화에서 "시간 잡아줘", "약속 시간 추천", "회의 조율", "이 캡처 보고 시간 정해줘" 같은 일정 조율 의도를 감지했을 때. 1:1 일정, ICS 파싱, 초대 발송, 비한국어 대화에는 사용하지 않는다.
---

# chat-schedule-resolver

## 1. 정의

3인 이상 단체가 단톡방 자연어 대화로 약속을 잡을 때 발생하는 의사결정
병목(메시지 핑퐁·응답률 하락·결정 누락)을 제거한다. 사용자를 도구로
이동시키지 않고, 이미 있는 자연어 대화의 맥락 안에서 결과를 도출한다.

비정형 자연어 대화 → 정형 일정 데이터 변환 + 캘린더 교집합 계산을
한 번의 호출로 완료한다. ICS 파싱·캘린더 등록·초대 발송은 본 Skill의
책임이 아니라, 호출 Agent가 결과 JSON을 받아 다음 단계에서 수행한다.

## 2. 호출 시점

다음 발화 패턴이 단톡방 캡처/텍스트와 함께 들어올 때 호출:

- "이 단톡 보고 시간 잡아줘"
- "다같이 가능한 시간 뽑아줘"
- "이 캡처에서 약속 시간 추천"
- "회의 시간 조율해줘"
- "/일정정해줘"

자동 호출(force_decide): 일정 조율 대화가 일정 횟수(예: 8회) 이상
지속되고 응답률이 60% 이하로 떨어진 것을 Agent가 감지했을 때.

## 3. 입력 JSON

제안서 2.3절 명세를 그대로 따른다.

```json
{
  "conversation": [
    {"user": "민지", "text": "이번주 다같이 보자", "ts": "2026-05-11T10:00"},
    {"user": "준호", "text": "금요일 저녁 좋아",   "ts": "2026-05-11T10:02"},
    {"user": "지수", "text": "토요일 낮은 안돼",   "ts": "2026-05-11T10:05"}
  ],
  "image_paths": ["assets/uploads/screenshot_1.png"],
  "participants": ["민지", "준호", "지수"],
  "calendars": {
    "민지": [{"start": "2026-05-15T18:00", "end": "2026-05-15T22:00"}],
    "준호": [{"start": "2026-05-15T19:00", "end": "2026-05-15T23:00"}],
    "지수": [{"start": "2026-05-15T19:00", "end": "2026-05-15T21:00"}]
  },
  "mode": "find",
  "top_n": 3
}
```

필드:
- `conversation`: `{user, text, ts}` 메시지 배열. (선택 — `image_paths`와
  최소 하나는 반드시 있어야 함.)
- `image_paths`: 단톡방 캡처(.png/.jpg/.jpeg/.heic) 경로 배열. (선택 —
  `conversation`과 최소 하나는 반드시 있어야 함.) 각 이미지는 Upstage OCR
  로 텍스트화한 뒤 Solar Pro 3 재포맷터로 내부적으로 `{user, text, ts}`
  메시지 배열로 변환되어 동일 파이프라인을 탄다(OCR'd 행의 `ts`는 캡처
  내 타임스탬프 문자열 또는 빈 문자열).
- `participants`: 발화자 정식 명단. 모든 입력 경로(`conversation[].user`,
  OCR 결과, `calendars` 키)와 일치해야 한다(별명 추론 없음).
- `calendars[name]`: 해당 참여자의 **빈 시간** 윈도 배열
  (`{start, end}` ISO 8601 local).
- `mode`: `"find"` | `"force_decide"`.
- `top_n`: 반환할 후보 수.

### 3.1 conversation ↔ image_paths 라우팅

| 입력 조합 | 처리 |
| --- | --- |
| `conversation`만 | 그대로 ① 시간 표현 추출 단계로 진입. |
| `image_paths`만 | 각 이미지 → OCR → Solar Pro 3 재포맷 → 내부 `conversation` 배열로 변환 후 동일 파이프라인. |
| 둘 다 존재 | **`conversation`이 권위(source of truth)**. `image_paths`는 OCR 결과를 별도 보관해 보조 검증 신호로 사용 — OCR 결과에는 등장하는데 `conversation`에 없는 메시지는 `unresolved.notes`로 surface(텍스트 누락 가능성 알림), 반대로 `conversation`에만 있는 메시지는 그대로 신뢰. 두 소스를 silently merge 하지 않는다. |
| 둘 다 없음 | `error` 반환(failure mode 참조). |

## 4. 출력 JSON

제안서 2.3절 명세를 그대로 따른다.

```json
{
  "recommended_slots": [
    {
      "start": "2026-05-15T19:00",
      "end":   "2026-05-15T21:00",
      "participants_available": ["민지", "준호", "지수"],
      "confidence": 0.92,
      "rationale": "지수가 '금요일 늦게' 선호. 모두 캘린더 비어있음."
    }
  ],
  "extracted_preferences": [
    {"who": "준호", "type": "exclude", "time": "토요일 낮은 안돼",
     "evidence_msg_id": 3, "grounded": true}
  ],
  "unresolved": []
}
```

언어 계약: `rationale`만 한국어 자연어. 그 외 키와 값은 영문/ISO.

`extracted_preferences` 항목 중 `grounded: false`는 결과에서 제외되며
`unresolved`로 이동한다(투명성을 위해 둘 다 노출).

## 5. 핵심 작동 로직 (제안서 2.2절)

```
[Input] image_paths[] (선택) + conversation[] (선택) + 캘린더 빈 시간
   │
   │ image_paths가 있으면:
   ▼
ⓞ Upstage OCR (model=ocr)  → 캡처 raw 텍스트
   + Solar Pro 3 재포맷    → {user, text, ts:""} 메시지 배열
   (conversation도 있으면 그것을 권위로 두고 OCR은 보조 신호)
   │
   ▼
① 비정형 텍스트의 데이터 치환
   "이번 주말", "금요일 늦게", "수업 끝나고" 등 모호한 한국어 시간 표현을
   정확한 날짜·시간 슬롯으로 환산
   │
   ▼
② 객체 간 교집합 연산
   참여자 캘린더 빈 시간 ∩ 추출된 가능 시간을 30분 단위 슬롯으로 계산
   │
   ▼
③ 근거 기반 랭킹
   가용 인원 수 / 평균 선호도(certainty) / 임박도
   │
   ▼
④ 환각 검증
   추출된 모든 선호 항목이 실제 대화 문장에 근거하는지 검증.
   근거 없는 항목은 결과에서 제외하고 unresolved에 기록.
   │
   ▼
[Output] recommended_slots + rationale + grounded 근거 (JSON)
```

## 6. Upstage API 매핑 (제안서 4장)

| API | 본 Skill에서의 역할 |
| --- | --- |
| Solar Pro 3 | ① 한국어 비정형 시간 표현 추론. "늦게"=20:00 이후, "이른 저녁"=18:00~19:30, "점심쯤"=11:30~13:30 같은 정량화 규칙을 system 프롬프트로 주입. |
| Information Extract | ① 스키마-aligned JSON 자동 추출(제안서 명시). |
| OCR | ⓪ 단톡방 캡처(.png/.jpg/.heic) → 발화자명+메시지 분리 추출. `image_paths` 입력이 있을 때 항상 실행. |
| Evidence Verification (Solar Pro 3 LLM-judge) | ④ 추출된 모든 선호가 실제 메시지에 근거하는지 boolean 검증. 제안서 4.2(d)의 환각 검증 단계를 Solar Pro 3 self-verification으로 구현. |
| Embeddings | (v2) 사용자별 응답 패턴/선호 시간대 누적 학습. |

### 6.1 v1 구현 메모 (제안서 대비 deviation)

다음 두 가지는 제안서 4장의 API 매핑을 그대로 구현할 수 없어 Solar Pro 3
로 우회한다. v1에서 의식적으로 받아들인 차이점이다.

- **Information Extract → Solar Pro 3 + `response_format=json_schema`**.
  IE 엔드포인트는 base64 image/PDF 문서만 받으며, plain text(`data:text/plain`)
  는 HTTP 400. 채팅 텍스트 입력 경로에서는 Solar Pro 3의 strict JSON
  schema 출력으로 동일 결과를 얻는다. 캡처 이미지 입력 경로에서도
  현재는 OCR → Solar 경유가 더 안정적이라 동일하게 처리한다.
- **Evidence Verification → Solar Pro 3 LLM-judge**. 제안서 4장에 명시된
  Upstage 전용 환각 검증 API(`solar-1-mini-*-check` 류)는 2026-05 기준
  공식 API 카탈로그(`console.upstage.ai/api/docs/for-agents/raw` — Chat /
  Embeddings / OCR / Parse / Classification / IE / Schema-Gen / Prebuilt /
  Agent / Doc Split / File Search 11개 섹션)에서 제거되어 호출 불가(모든
  후보 모델명이 `400 invalid_request_body: model is invalid or no longer
  supported`; `langchain_upstage 0.7.7`도 관련 클래스를 제거).
  본 Skill은 동일 의미를 Solar Pro 3 self-verification으로 구현한다. `UpstageClient.verify_evidence(context, claim)`이 다음 strict
  JSON 스키마로 호출:

  ```json
  {"evidence_verified": bool,
   "supporting_msg_ids": [int, ...],
   "reason": "한 줄 한국어 사유"}
  ```

  `temperature=0`, `response_format=json_schema`, system prompt 절대 규칙:
  "context 외부 지식 사용 금지, 경계 사례는 보수적으로 false".
  외부 노출 `extracted_preferences[i].grounded` 필드는
  `evidence_verified` 값을 그대로 반영한다.

## 7. 내부 처리 상세

### 7.1 추출 스키마 (Solar Pro 3 structured output)

`response_format.json_schema.schema`에 전달. `strict: true`,
`additionalProperties: false` 필수.

다중 일자 표현은 (날짜 × 시간대) 단위로 한 행씩 평탄화한다.
`"다음주 점심쯤"` → 7행, `"주말"` → 2행(토+일).

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
          "who":             {"type": "string"},
          "type":            {"type": "string", "enum": ["prefer", "exclude"]},
          "time":            {"type": "string"},
          "start":           {"type": "string", "description": "ISO 8601 local, inclusive"},
          "end":             {"type": "string", "description": "ISO 8601 local, exclusive; use next-day 00:00 not '24:00'"},
          "certainty":       {"type": "number"},
          "evidence_msg_id": {"type": "integer", "description": "1-based index into conversation[]"}
        },
        "required": ["who", "type", "time", "start", "end", "certainty", "evidence_msg_id"],
        "additionalProperties": false
      }
    }
  },
  "required": ["participants", "items"],
  "additionalProperties": false
}
```

`start`/`end`/`certainty`는 내부 계산용으로 추출하되, 최종
`extracted_preferences` 출력에는 제안서 명세대로 `who, type, time,
evidence_msg_id, grounded`만 노출한다. 내부 trace가 필요한 호출자는
`scripts/pipeline.py`의 중간 결과를 사용한다.

### 7.2 Evidence Verification 게이트

각 `(who, time)` 쌍에 대해 `verify_evidence(context, claim)` 1회 호출
(쌍 단위 — 한 phrase가 여러 날짜 행으로 평탄화되므로 행 단위 호출은 낭비).
`evidence_verified == false`인 모든 행은 `unresolved`로 이동.
판정 근거는 `judge_response`(파싱된 dict)로 행에 첨부되어 디버깅/감사에
활용 가능. `supporting_msg_ids` 첫 원소가 외부 노출용
`extracted_preferences[i].evidence_msg_id`로 매핑된다(빈 배열이면 원본
Step 2의 `evidence_msg_id`를 그대로 보존).

### 7.3 교집합

1. 각 참여자의 `calendars[]` 빈 시간을 30분 슬롯으로 분할.
2. 각 참여자의 grounded `prefer` 윈도 합집합과 AND
   (선호 항목이 없는 참여자는 캘린더 빈 시간 자체를 가용으로 간주).
3. 각 참여자의 grounded `exclude` 윈도 합집합을 차집합.
4. 연속 슬롯을 다시 인터벌로 병합 후 랭킹 단계로.

### 7.4 랭킹 (제안서 3.2절 동일)

1. `participants_available` 수 (내림차순)
2. 지지 `prefer` 행들의 평균 `certainty` (내림차순)
3. 임박도 (오름차순; 가까운 미래 가산)

`top_n` 만큼 emit. 모두 가능한 슬롯이 없으면 부분 매칭이라도 반환하며,
이때도 `participants_available`에 가능한 사람만 나열한다.

### 7.5 force_decide 모드

제안서 시나리오 B: 30분 무응답 시 자동 확정·캘린더 등록은 외부 Agent의
타이머/등록 책임이다. 본 Skill은 `mode == "force_decide"`로 호출되면
동일한 Top-N을 반환하되, 호출 Agent가 1순위 슬롯을 "자동 확정 후보"로
취급하면 된다(별도 플래그를 추가하지 않고 1순위 슬롯을 그대로 사용).

## 8. 실패 모드

- **`conversation`도 `image_paths`도 없음**: `recommended_slots: []`
  + `error: "Provide conversation or image_paths (or both)"` 즉시 반환.
- **`image_paths` 중 일부만 OCR 실패**: 성공한 캡처만 사용, 실패한
  경로는 `unresolved.notes`에 `{image_path, reason}`로 기록(전체 실패가
  아니면 예외 미발생).
- **OCR 결과 전부 비어있음** (그리고 `conversation`도 없음):
  `recommended_slots: []` + `error` 필드.
- **`conversation`과 OCR 결과 충돌**: `conversation` 우선. OCR에만
  보이는 메시지는 `unresolved.notes`에 `{source: "ocr_only",
  image_path, text}`로 surface하되 추출에는 사용하지 않음.
- **참여자 이름 불일치** (`participants` ↔ `conversation[].user` ↔
  OCR'd user ↔ `calendars` 키): `unresolved`에 사유와 함께 기록.
- **교집합 0**: 부분 매칭 후보로 반환(예외 미발생).
- **`UPSTAGE_API_KEY` 누락**: 시작 시 fail-fast
  (`scripts/upstage_client.py`).

## 9. v2 로드맵 (제안서 5.2/5.3)

Embeddings 기반 사용자별 응답 패턴/선호 시간대 학습은 `embeddings/`
하위에 자리만 마련해 두었다(현재 비어있음). v1 MVP 일정(2026-05-27)
유지를 위해 미연결.

상위 Agent 묶음(미팅 매니저 Agent)에서 본 Skill은 시간 후보 노드를
담당하며, 위치 추천·어젠다 생성·참여 패턴 학습·사후 요약은 별개
Skill로 분리된다.

## 10. 프로젝트 구조

```
chat-schedule-resolver/
├── SKILL.md
├── assets/
│   ├── .env.example       # template; copy to .env and fill UPSTAGE_API_KEY
│   └── .env               # (gitignored — user-created)
├── scripts/
│   ├── upstage_client.py
│   ├── step2_normalize.py
│   ├── step3_verify.py
│   ├── step4_ocr_ingest.py
│   ├── step5_integration.py
│   └── pipeline.py
├── references/
│   ├── upstage-ocr.md
│   ├── upstage-information-extract.md
│   └── upstage-chat.md
└── embeddings/            # v2 placeholder; do not use in v1
    └── README.md
```
