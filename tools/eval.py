"""LLM evaluation harness: how well are operator notes interpreted?

    python -m tools.eval                          # all evals/*.jsonl, real provider from .env
    python -m tools.eval --repeat 3               # + consistency across runs
    python -m tools.eval --model gpt-5.4-nano     # A/B another model
    python -m tools.eval --files paraphrases distractors --no-crosscheck

Each JSONL line: {"id", "family", "capacity_kwh", "note", "expected": {"directive_type",
"structured_adjustment"}}. Scoring uses the same machine-checked fields as the judge.
Deploy gates: public_cases 100%, paraphrases >= 95%, distractors 100%, adversarial 100%.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter, defaultdict
from typing import Any

from app.config import Settings
from app.llm import InterpretResult, NoteInterpreter, build_providers
from app.metrics import Metrics
from app.schemas import DirectiveInterpretation
from tools.common import EVALS, interpretation_matches, percentile, table, utf8_stdout

GATES = {"public_cases": 1.0, "paraphrases": 0.95, "distractors": 1.0, "adversarial": 1.0}


def load(files: list[str]) -> list[dict[str, Any]]:
    records = []
    for path in sorted(EVALS.glob("*.jsonl")):
        if files and path.stem not in files:
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line) | {"file": path.stem})
    return records


def score(record: dict[str, Any], result: InterpretResult) -> dict[str, bool]:
    actual = DirectiveInterpretation.build(0, result.directive, "").model_dump()
    expected = record["expected"]
    truth = {"applies": expected["directive_type"] != "no_op", **expected}
    ours, theirs = actual["structured_adjustment"] or {}, truth["structured_adjustment"] or {}
    return {
        "applies": actual["applies"] == truth["applies"],
        "type": actual["directive_type"] == truth["directive_type"],
        "hours": ours.get("hours") == theirs.get("hours"),
        "full": interpretation_matches(actual, truth),
    }


async def run(args: argparse.Namespace) -> int:
    overrides: dict[str, Any] = {"cache_max_entries": 0}
    if args.model:
        overrides["openai_model"] = args.model
    if args.no_crosscheck:
        overrides["crosscheck_enabled"] = False
    settings = Settings(**overrides)
    records = load(args.files)
    if not records:
        print("no eval records found")
        return 1

    metrics = Metrics()
    interpreter = NoteInterpreter(settings, build_providers(settings, metrics), metrics)
    if interpreter.degraded:
        print("WARNING: no LLM key configured - this run scores the deterministic reader only.\n")
    else:
        print(f"providers: {settings.configured_providers()}\n")

    runs: list[list[InterpretResult]] = []
    for _ in range(args.repeat):
        runs.append(
            list(
                await asyncio.gather(
                    *(
                        interpreter.interpret_note(r["note"], 0, float(r["capacity_kwh"]))
                        for r in records
                    )
                )
            )
        )

    results = runs[0]
    scores = [score(record, result) for record, result in zip(records, results, strict=True)]
    groups: dict[str, list[int]] = defaultdict(list)
    for index, record in enumerate(records):
        groups[record["file"]].append(index)
        groups[f"  {record['file']}/{record['family']}"].append(index)

    def rate(indexes: list[int], key: str) -> str:
        return f"{100 * sum(scores[i][key] for i in indexes) / len(indexes):.0f}%"

    rows = [
        [name, str(len(ix)), rate(ix, "applies"), rate(ix, "type"), rate(ix, "hours"),
         rate(ix, "full")]
        for name, ix in sorted(groups.items(), key=lambda kv: kv[0].strip())
    ]  # fmt: skip
    everything = list(range(len(records)))
    rows.append(["TOTAL", str(len(records)), *(rate(everything, k) for k in
                                               ("applies", "type", "hours", "full"))])  # fmt: skip
    print(table(["set / family", "n", "applies", "type", "hours", "full match"], rows))

    confusion = Counter(
        (record["expected"]["directive_type"], result.directive.directive_type)
        for record, result in zip(records, results, strict=True)
        if record["expected"]["directive_type"] != result.directive.directive_type
    )
    if confusion:
        print("\ntype confusion (expected -> got):")
        for (expected, got), count in confusion.most_common():
            print(f"  {expected} -> {got}: {count}")

    failures = [i for i in everything if not scores[i]["full"]]
    if failures:
        print(f"\nfailures ({len(failures)}):")
        for i in failures:
            entry = DirectiveInterpretation.build(0, results[i].directive, "")
            print(f"  [{records[i]['id']}] {records[i]['note'][:110]}")
            print(f"      expected {records[i]['expected']}")
            print(f"      got      {entry.directive_type} {entry.structured_adjustment} "
                  f"via {results[i].provider}")  # fmt: skip

    latencies = [r.latency_ms for r in results]
    providers = Counter(r.provider for r in results)
    print(f"\nproviders used: {dict(providers)}")
    print(f"guardrail: {dict(Counter(r.guardrail for r in results))}")
    print(f"latency per note: p50 {percentile(latencies, 0.5):.0f} ms | "
          f"p95 {percentile(latencies, 0.95):.0f} ms")  # fmt: skip
    counters = metrics.snapshot()["counters"]
    print(f"tokens in/out: {counters.get('tokens_in', 0)}/{counters.get('tokens_out', 0)} | "
          f"cross-check re-asks: {counters.get('crosscheck_reasks', 0)} "
          f"(changed {counters.get('crosscheck_changed', 0)})")  # fmt: skip

    if args.repeat > 1:
        stable = sum(len({(run[i].directive) for run in runs}) == 1 for i in everything)
        print(f"consistency over {args.repeat} runs: {stable}/{len(records)} notes identical")

    gate_failed = False
    print()
    for name, threshold in GATES.items():
        indexes = groups.get(name)
        if not indexes:
            continue
        achieved = sum(scores[i]["full"] for i in indexes) / len(indexes)
        ok = achieved >= threshold
        gate_failed |= not ok
        print(f"gate {name:<13} {achieved:6.1%} (need {threshold:.0%}) -> "
              f"{'PASS' if ok else 'FAIL'}")  # fmt: skip
    return 1 if gate_failed else 0


def main() -> int:
    utf8_stdout()
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--files", nargs="*", default=[], help="eval set names (default: all)")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--model", help="override OPENAI_MODEL for this run")
    parser.add_argument("--no-crosscheck", action="store_true")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
