"""Step 1 unit tests: Solar Pro 3 first-pass time-preference inference.

Runs 3 small Korean group-chat cases through ``UpstageClient.infer_time_preferences``
and prints the raw assistant output for each. These are smoke / sanity checks --
they hit the real Upstage API, so they require ``UPSTAGE_API_KEY`` in
``assets/.env``.

Pass criteria (per case, human-judged):
  1. Every speaker who mentioned timing appears in the output.
  2. Vague Korean phrases ("늦게", "저녁", "점심쯤") are pinned to the documented
     hour ranges, not invented ones.
  3. Relative dates ("이번주 금요일", "내일") are resolved against the supplied
     ``reference_date``.

Usage:
    python3 scripts/test_step1.py
"""

from __future__ import annotations

import sys
import textwrap

from upstage_client import UpstageClient


CASES = [
    {
        "name": "SKILL.md example (3-person, mixed prefer/exclude)",
        "reference_date": "2026-05-11",
        "conversation": textwrap.dedent(
            """\
            민지: 이번주 늦게 보자
            준호: 금요일 저녁 좋아
            지수: 토요일 낮은 안돼
            """
        ),
        "expect_speakers": ["민지", "준호", "지수"],
    },
    {
        "name": "2-person, lunch + weekday constraint",
        "reference_date": "2026-05-11",
        "conversation": textwrap.dedent(
            """\
            수아: 다음주 점심쯤 어때
            현우: 월요일이랑 화요일은 안돼
            """
        ),
        "expect_speakers": ["수아", "현우"],
    },
    {
        "name": "Single hard constraint (exclude only)",
        "reference_date": "2026-05-11",
        "conversation": textwrap.dedent(
            """\
            태윤: 주말은 절대 안돼
            """
        ),
        "expect_speakers": ["태윤"],
    },
]


def run_case(client: UpstageClient, case: dict) -> bool:
    print("=" * 72)
    print(f"CASE: {case['name']}")
    print(f"reference_date: {case['reference_date']}")
    print("-- conversation --")
    print(case["conversation"].rstrip())
    print("-- assistant output --")
    try:
        out = client.infer_time_preferences(
            case["conversation"], reference_date=case["reference_date"]
        )
    except Exception as e:
        print(f"[ERROR] {type(e).__name__}: {e}")
        return False
    print(out)
    print("-- speaker presence check --")
    missing = [s for s in case["expect_speakers"] if s not in out]
    if missing:
        print(f"[FAIL] missing speakers: {missing}")
        return False
    print("[OK] all expected speakers present in output")
    return True


def main() -> int:
    client = UpstageClient()
    results = [run_case(client, c) for c in CASES]
    print("=" * 72)
    passed = sum(results)
    print(f"SUMMARY: {passed}/{len(results)} cases passed speaker-presence check")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
