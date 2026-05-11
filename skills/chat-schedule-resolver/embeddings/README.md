# Embeddings (v2 roadmap)

Reserved for v2: per-user response pattern learning via Upstage Embeddings
(`embedding-query` / `embedding-passage`).

Not used in v1 (MVP). Kept as a placeholder so the v2 extension lands without
re-shaping the skill directory.

## Planned contents

- `index.jsonl` — appended embeddings of past `{user, utterance, resolved_slot}` triples.
- `retriever.py` — k-NN lookup over `index.jsonl` to bias slot ranking by user
  history (e.g. user X consistently picks evening slots).

## Why deferred

- Embeddings need accumulated data to be meaningful; insufficient at competition
  time (2026-05-27).
- v1 must hit the 30-point technical-completeness bar first; v2 lands as the
  "Agent-service extension" bonus.
