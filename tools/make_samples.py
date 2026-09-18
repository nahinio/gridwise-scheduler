"""Split the official public case pack into one request file per case.

Usage: python -m tools.make_samples
Writes samples/SAMPLE-XX.input.json (the exact `input` object of each public case).
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACK = ROOT / "docs" / "official" / "BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json"
OUT = ROOT / "samples"


def main() -> None:
    cases = json.loads(PACK.read_text(encoding="utf-8"))["cases"]
    OUT.mkdir(exist_ok=True)
    for case in cases:
        path = OUT / f"{case['id']}.input.json"
        path.write_text(json.dumps(case["input"], indent=2) + "\n", encoding="utf-8")
    print(f"wrote {len(cases)} sample inputs to {OUT}")


if __name__ == "__main__":
    main()
