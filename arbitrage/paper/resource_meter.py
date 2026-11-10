"""Lightweight per-venue resource accounting for the live simulation."""
from __future__ import annotations

import math
import resource
import sys
import time
from collections import defaultdict


def _percentile(sorted_vals: list[float], q: float) -> float:
    """Nearest-rank percentile (q in 0..1) of a non-empty sorted list."""
    idx = max(math.ceil(q * len(sorted_vals)) - 1, 0)
    return float(sorted_vals[idx])


class ResourceMeter:
    def __init__(self, max_latency_samples: int = 10_000) -> None:
        self._max = max_latency_samples
        self._latency: dict[str, list[float]] = defaultdict(list)
        self._api_calls: dict[str, int] = defaultdict(int)
        self._ws_messages: dict[str, int] = defaultdict(int)
        self._start = time.monotonic()

    def record_quote_latency_ms(self, venue: str, latency: float) -> None:
        samples = self._latency[venue]
        samples.append(float(latency))
        if len(samples) > self._max:
            del samples[: len(samples) - self._max]

    def record_api_call(self, venue: str) -> None:
        self._api_calls[venue] += 1

    def record_ws_message(self, venue: str) -> None:
        self._ws_messages[venue] += 1

    def snapshot(self) -> dict:
        elapsed = max(time.monotonic() - self._start, 1e-9)
        out: dict = {}
        venues = set(self._latency) | set(self._api_calls) | set(self._ws_messages)
        for v in sorted(venues):
            lat = sorted(self._latency.get(v, ()))
            ws = self._ws_messages.get(v, 0)
            out[v] = {
                "latency_p50_ms": _percentile(lat, 0.50) if lat else None,
                "latency_p95_ms": _percentile(lat, 0.95) if lat else None,
                "latency_p99_ms": _percentile(lat, 0.99) if lat else None,
                "ws_messages": ws,
                "ws_msgs_per_s": ws / elapsed,
                "api_calls": self._api_calls.get(v, 0),
            }
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        out["process"] = {
            "cpu_seconds": time.process_time(),
            # ru_maxrss is bytes on macOS, kilobytes on Linux
            "max_rss_kb": rss // 1024 if sys.platform == "darwin" else rss,
        }
        return out
