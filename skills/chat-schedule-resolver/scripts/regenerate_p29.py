"""One-off: re-synthesize p29 (speaker confusion in v1) keeping the same spec.

The v1 p29 had 팀장님 attributing a 'busy on Wednesday' statement to 민수
when it was actually 지영. Re-running with the same spec parameters and
a fresh sampling seed should give a cleaner version.

Writes the new record in place inside golden_30.jsonl (line-by-line
rewrite preserving every other scenario).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from generate_golden_30 import (
    JSONL_PATH,
    REVIEW_PATH,
    SPECS,
    _generate_one,
    _render_review,
    _validate,
)
from upstage_client import UpstageClient


TARGET_ID = "p29"


def main() -> int:
    spec = next(s for s in SPECS if s.scenario_id == TARGET_ID)
    client = UpstageClient()

    print(f"Re-synthesising {TARGET_ID} ({spec.topic}, n={spec.people_n}, "
          f"size={spec.msg_size}, ambig={spec.ambiguity}, conflict={spec.conflict})")
    scenario = _generate_one(client, spec)
    issues = _validate(spec, scenario)
    if issues:
        print(f"  ⚠ {issues}")

    # Rewrite the line in JSONL.
    lines = JSONL_PATH.read_text().splitlines()
    new_lines: list[str] = []
    replaced = False
    for line in lines:
        rec = json.loads(line)
        if rec["scenario_id"] == TARGET_ID:
            rec["scenario"] = scenario
            new_lines.append(json.dumps(rec, ensure_ascii=False))
            replaced = True
        else:
            new_lines.append(line)
    if not replaced:
        print(f"[ERR] {TARGET_ID} not found in {JSONL_PATH}")
        return 1
    JSONL_PATH.write_text("\n".join(new_lines) + "\n")

    # Rebuild the review.md so the reviewer sees the new content.
    records = []
    for line in new_lines:
        rec = json.loads(line)
        sspec = rec["spec"]
        scen = rec["scenario"]
        # Re-derive a dataclass-shaped spec for _render_review (uses dict access).
        records.append({"spec": sspec, "scenario": scen, "issues": []})
    REVIEW_PATH.write_text(_render_review(records), encoding="utf-8")

    print()
    print("Re-synthesised conversation:")
    for i, m in enumerate(scenario["conversation"], start=1):
        print(f"  {i:>2}. {m['user']} ({m['ts']}) — {m['text']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
