"""In-process counters and latency percentiles for GET /stats. Aggregates only - never
note text, scenario content, or anything secret. Resets on restart."""

from __future__ import annotations

import time
from collections import Counter, deque
from typing import Any


class Metrics:
    def __init__(self, reservoir: int = 1000) -> None:
        self._started = time.time()
        self._counters: Counter[str] = Counter()
        self._latency: dict[str, deque[float]] = {}
        self._reservoir = reservoir

    def inc(self, name: str, label: str | None = None, amount: int = 1) -> None:
        self._counters[f"{name}.{label}" if label else name] += amount

    def observe(self, stage: str, milliseconds: float) -> None:
        self._latency.setdefault(stage, deque(maxlen=self._reservoir)).append(milliseconds)

    def count(self, name: str, label: str | None = None) -> int:
        return self._counters[f"{name}.{label}" if label else name]

    @staticmethod
    def _percentile(ordered: list[float], q: float) -> float:
        return round(ordered[min(len(ordered) - 1, int(q * len(ordered)))], 2)

    def snapshot(self) -> dict[str, Any]:
        latency = {}
        for stage, samples in self._latency.items():
            ordered = sorted(samples)
            latency[stage] = {
                "count": len(ordered),
                "p50_ms": self._percentile(ordered, 0.50),
                "p95_ms": self._percentile(ordered, 0.95),
                "max_ms": round(ordered[-1], 2),
            }
        return {
            "uptime_s": round(time.time() - self._started, 1),
            "counters": dict(sorted(self._counters.items())),
            "latency": latency,
        }
