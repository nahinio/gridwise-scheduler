"""Helpers shared by the command-line tools."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
PACK = ROOT / "docs" / "official" / "BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json"
SAMPLES = ROOT / "samples"
EVALS = ROOT / "evals"
NUMERIC_TOLERANCE = 0.01


def utf8_stdout() -> None:
    """Windows consoles default to cp1252; notes may contain Bangla and emoji."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def load_public_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = json.loads(PACK.read_text(encoding="utf-8"))["cases"]
    return cases


def interpretation_matches(actual: dict[str, Any], expected: dict[str, Any]) -> bool:
    """Machine-checked fields only: applies, type, exact hours, numerics within 0.01, shape."""
    if actual.get("applies") != expected["applies"]:
        return False
    if actual.get("directive_type") != expected["directive_type"]:
        return False
    ours, theirs = actual.get("structured_adjustment"), expected["structured_adjustment"]
    if ours is None or theirs is None:
        return ours is None and theirs is None
    if set(ours) != set(theirs) or ours["hours"] != theirs["hours"]:
        return False
    return all(
        abs(float(ours[key]) - float(theirs[key])) <= NUMERIC_TOLERANCE
        for key in theirs
        if key != "hours"
    )


def percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))] if ordered else 0.0


def table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [max(len(str(cell)) for cell in column) for column in zip(headers, *rows, strict=True)]
    line = "  ".join("{:<" + str(width) + "}" for width in widths)
    rule = "  ".join("-" * width for width in widths)
    return "\n".join([line.format(*headers), rule, *(line.format(*row) for row in rows)])
