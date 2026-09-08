"""Online per-segment travel-time estimator. This replaces any belt-speed input.

For each segment (pair of adjacent counting lines) we keep a rolling window of Δt values
from successfully matched plates and use the median as the current estimate. This auto-adapts
to belt speed changes and track curvature. Before enough samples exist we fall back to a seed
derived from the map's `approx_loop_seconds` split by segment arc-length.
"""
from __future__ import annotations

import statistics
from collections import deque
from dataclasses import dataclass, field


@dataclass(slots=True)
class SegmentEstimator:
    seed_s: float
    window: int = 50
    _samples: deque[float] = field(default_factory=lambda: deque(maxlen=50))

    def observe(self, dt: float) -> None:
        if dt > 0:
            self._samples.append(dt)

    @property
    def n(self) -> int:
        return len(self._samples)

    def estimate(self) -> float:
        if self.n < 3:
            return self.seed_s
        return statistics.median(self._samples)

    def match_window(self, base_tolerance_s: float) -> tuple[float, float]:
        """Expected Δt +/- tolerance (widened while under-sampled)."""
        est = self.estimate()
        slack = base_tolerance_s * (2.0 if self.n < 10 else 1.0)
        if self.n >= 5:
            slack = max(slack, statistics.pstdev(self._samples) * 3)
        return (max(0.0, est - slack), est + slack)


class TravelTimeModel:
    def __init__(self) -> None:
        self._by_seg: dict[tuple[str, str], SegmentEstimator] = {}

    def seed_from_map(
        self, segments: list[tuple[str, str, float]], approx_loop_seconds: float | None
    ) -> None:
        """segments: (upstream_id, downstream_id, normalised_gap_len)."""
        loop = approx_loop_seconds or 120.0
        for up, dn, gap in segments:
            self._by_seg[(up, dn)] = SegmentEstimator(seed_s=max(1.0, loop * gap))

    def get(self, upstream_id: str, downstream_id: str) -> SegmentEstimator:
        key = (upstream_id, downstream_id)
        if key not in self._by_seg:
            self._by_seg[key] = SegmentEstimator(seed_s=10.0)
        return self._by_seg[key]

    def loop_seconds_estimate(self) -> float:
        return sum(e.estimate() for e in self._by_seg.values()) or 0.0

    def snapshot(self) -> dict:
        return {f"{k[0]}>{k[1]}": list(v._samples) for k, v in self._by_seg.items()}

    def restore(self, data: dict) -> None:
        for k, samples in data.items():
            up, dn = k.split(">")
            est = self.get(up, dn)
            est._samples.extend(samples)
