"""Derive repo fixtures from the official public case pack.

Usage: python -m tools.make_samples
Writes samples/SAMPLE-XX.input.json (the exact `input` object of each public case) and
evals/public_cases.jsonl (every public note with its expected interpretation).
"""

from __future__ import annotations

import json

from tools.common import EVALS, SAMPLES, load_public_cases


def main() -> None:
    cases = load_public_cases()
    SAMPLES.mkdir(exist_ok=True)
    EVALS.mkdir(exist_ok=True)
    lines = []
    for case in cases:
        path = SAMPLES / f"{case['id']}.input.json"
        path.write_text(json.dumps(case["input"], indent=2) + "\n", encoding="utf-8", newline="\n")
        notes = case["input"]["operator_notes"]
        expected = case["expected_output"]["directive_interpretation"]
        for note, entry in zip(notes, expected, strict=True):
            lines.append(
                json.dumps(
                    {
                        "id": f"{case['id']}-n{entry['note_index']}",
                        "family": "public",
                        "capacity_kwh": case["input"]["battery"]["capacity_kwh"],
                        "note": note,
                        "expected": {
                            "directive_type": entry["directive_type"],
                            "structured_adjustment": entry["structured_adjustment"],
                        },
                    },
                    ensure_ascii=False,
                )
            )
    (EVALS / "public_cases.jsonl").write_text(
        "\n".join(lines) + "\n", encoding="utf-8", newline="\n"
    )
    print(f"wrote {len(cases)} sample inputs and {len(lines)} public eval notes")


if __name__ == "__main__":
    main()
