"""Concurrency test: p50 / p95 / max latency and error count under parallel load.

    python -m tools.load --url http://localhost:8000 --concurrency 20 --n 100

Pass criteria mirror the rubric: zero failed requests and p95 <= 5 s.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from typing import Any

import httpx

from tools.common import load_public_cases, percentile, utf8_stdout

P95_TARGET_S = 5.0


def unique_copy(scenario: dict[str, Any], index: int) -> dict[str, Any]:
    """Same scenario, but every note is textually unique: defeats the cache and single-flight,
    so each request really costs one LLM call per note (the hidden-set worst case)."""
    notes = [
        f"{note} (work order {index}-{n})" for n, note in enumerate(scenario["operator_notes"])
    ]
    return {**scenario, "operator_notes": notes}


async def run(url: str, concurrency: int, total: int, timeout: float, unique: bool) -> int:
    inputs = [case["input"] for case in load_public_cases()]
    gate = asyncio.Semaphore(concurrency)
    latencies: list[float] = []
    errors: list[str] = []

    async with httpx.AsyncClient(
        base_url=url.rstrip("/"), timeout=timeout, limits=httpx.Limits(max_connections=concurrency)
    ) as client:

        async def one(index: int) -> None:
            async with gate:
                started = time.perf_counter()
                try:
                    scenario = inputs[index % len(inputs)]
                    response = await client.post(
                        "/optimize-energy",
                        json=unique_copy(scenario, index) if unique else scenario,
                    )
                    if response.status_code != 200:
                        errors.append(f"HTTP {response.status_code}")
                    elif len(response.json()["hourly_plan"]) != 24:
                        errors.append("incomplete plan")
                except (httpx.HTTPError, ValueError, KeyError) as error:
                    errors.append(type(error).__name__)
                latencies.append(time.perf_counter() - started)

        wall = time.perf_counter()
        await asyncio.gather(*(one(i) for i in range(total)))
        wall = time.perf_counter() - wall

    p95 = percentile(latencies, 0.95)
    print(f"requests {total} | concurrency {concurrency} | wall {wall:.1f} s "
          f"| {total / wall:.1f} req/s")  # fmt: skip
    print(f"p50 {percentile(latencies, 0.5) * 1000:.0f} ms | p95 {p95 * 1000:.0f} ms "
          f"| max {max(latencies) * 1000:.0f} ms")  # fmt: skip
    print(f"errors {len(errors)}" + (f" -> {sorted(set(errors))}" if errors else ""))
    passed = not errors and p95 <= P95_TARGET_S
    print("RESULT:", "PASS" if passed else "FAIL")
    return 0 if passed else 1


def main() -> int:
    utf8_stdout()
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--n", type=int, default=100, help="total requests")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument(
        "--unique-notes", action="store_true", help="make every note unique (no cache help)"
    )
    args = parser.parse_args()
    return asyncio.run(run(args.url, args.concurrency, args.n, args.timeout, args.unique_notes))


if __name__ == "__main__":
    sys.exit(main())
