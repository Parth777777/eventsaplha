"""
Lightweight in-process metrics registry.

Exposes counters, gauges, histograms. Rendered in Prometheus exposition
format by `render_prometheus()`. Importable from both scraper and backend.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from typing import Dict, Iterable, List, Optional, Tuple


class _Counter:
    def __init__(self):
        self.value: float = 0.0


class _Gauge:
    def __init__(self):
        self.value: float = 0.0


class _Histogram:
    def __init__(self, buckets: Iterable[float]):
        self.buckets = list(buckets)
        self.counts = [0 for _ in self.buckets] + [0]  # +inf bucket
        self.sum: float = 0.0
        self.count: int = 0

    def observe(self, v: float) -> None:
        self.sum += v
        self.count += 1
        for i, b in enumerate(self.buckets):
            if v <= b:
                self.counts[i] += 1
                return
        self.counts[-1] += 1


class MetricRegistry:
    def __init__(self):
        self._lock = threading.Lock()
        self._counters: Dict[Tuple[str, Tuple[Tuple[str, str], ...]], _Counter] = defaultdict(_Counter)
        self._gauges: Dict[Tuple[str, Tuple[Tuple[str, str], ...]], _Gauge] = defaultdict(_Gauge)
        self._histograms: Dict[Tuple[str, Tuple[Tuple[str, str], ...]], _Histogram] = {}
        self._start = time.time()

    @staticmethod
    def _label_key(labels: Optional[Dict[str, str]]) -> Tuple[Tuple[str, str], ...]:
        return tuple(sorted((labels or {}).items()))

    def inc(self, name: str, value: float = 1.0, labels: Optional[Dict[str, str]] = None) -> None:
        with self._lock:
            self._counters[(name, self._label_key(labels))].value += value

    def set(self, name: str, value: float, labels: Optional[Dict[str, str]] = None) -> None:
        with self._lock:
            self._gauges[(name, self._label_key(labels))].value = value

    def observe(self, name: str, value: float, labels: Optional[Dict[str, str]] = None,
                buckets: Iterable[float] = (0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30)) -> None:
        key = (name, self._label_key(labels))
        with self._lock:
            h = self._histograms.get(key)
            if h is None:
                h = _Histogram(buckets)
                self._histograms[key] = h
            h.observe(value)

    def uptime_seconds(self) -> float:
        return time.time() - self._start

    def render_prometheus(self) -> str:
        lines: List[str] = []
        with self._lock:
            for (name, labels), c in self._counters.items():
                lines.append(f"# TYPE {name} counter")
                lines.append(f"{_format(name, labels)} {c.value}")
            for (name, labels), g in self._gauges.items():
                lines.append(f"# TYPE {name} gauge")
                lines.append(f"{_format(name, labels)} {g.value}")
            for (name, labels), h in self._histograms.items():
                lines.append(f"# TYPE {name} histogram")
                cumulative = 0
                for b, count in zip(h.buckets + [float("inf")], h.counts):
                    cumulative += count
                    bl = dict(labels)
                    bl["le"] = "+Inf" if b == float("inf") else str(b)
                    lines.append(f"{_format(name + '_bucket', tuple(sorted(bl.items())))} {cumulative}")
                lines.append(f"{_format(name + '_sum', labels)} {h.sum}")
                lines.append(f"{_format(name + '_count', labels)} {h.count}")
            lines.append(f"# TYPE app_uptime_seconds gauge")
            lines.append(f"app_uptime_seconds {self.uptime_seconds()}")
        return "\n".join(lines) + "\n"


def _format(name: str, labels: Tuple[Tuple[str, str], ...]) -> str:
    if not labels:
        return name
    body = ",".join(f'{k}="{_escape(v)}"' for k, v in labels)
    return f"{name}{{{body}}}"


def _escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


_REGISTRY = MetricRegistry()


def get_registry() -> MetricRegistry:
    return _REGISTRY


# Convenience shortcuts
def inc(name: str, value: float = 1.0, **labels: str) -> None:
    _REGISTRY.inc(name, value, labels or None)


def gauge(name: str, value: float, **labels: str) -> None:
    _REGISTRY.set(name, value, labels or None)


def observe(name: str, value: float, **labels: str) -> None:
    _REGISTRY.observe(name, value, labels or None)
