"""Pure track geometry: pixel polyline <-> normalised arc-length s in [0, 1).

The map editor gives us `path_px` (clicked points on the floor-plan image, ordered along
travel direction, optionally closed). Everything downstream works in `s`, the normalised
distance travelled along that path. This module is pure and fully unit-tested.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

Point = tuple[float, float]


def _catmull_rom(p0: Point, p1: Point, p2: Point, p3: Point, n: int) -> list[Point]:
    out: list[Point] = []
    for i in range(n):
        t = i / n
        t2, t3 = t * t, t * t * t
        out.append(
            (
                0.5
                * (
                    (2 * p1[0])
                    + (-p0[0] + p2[0]) * t
                    + (2 * p0[0] - 5 * p1[0] + 4 * p2[0] - p3[0]) * t2
                    + (-p0[0] + 3 * p1[0] - 3 * p2[0] + p3[0]) * t3
                ),
                0.5
                * (
                    (2 * p1[1])
                    + (-p0[1] + p2[1]) * t
                    + (2 * p0[1] - 5 * p1[1] + 4 * p2[1] - p3[1]) * t2
                    + (-p0[1] + 3 * p1[1] - 3 * p2[1] + p3[1]) * t3
                ),
            )
        )
    return out


def densify(path_px: list[Point], *, closed: bool, samples_per_seg: int = 24) -> list[Point]:
    """Catmull-Rom spline through the clicked points, sampled to a fine polyline."""
    if len(path_px) < 3:
        return list(path_px)
    pts = list(path_px)
    n = len(pts)
    fine: list[Point] = []
    last = n if closed else n - 1
    for i in range(last):
        p0 = pts[(i - 1) % n] if closed else pts[max(i - 1, 0)]
        p1 = pts[i % n]
        p2 = pts[(i + 1) % n] if closed else pts[min(i + 1, n - 1)]
        p3 = pts[(i + 2) % n] if closed else pts[min(i + 2, n - 1)]
        fine.extend(_catmull_rom(p0, p1, p2, p3, samples_per_seg))
    if not closed:
        fine.append(pts[-1])
    return fine


@dataclass(slots=True)
class TrackGeometry:
    """Immutable arc-length model of the track."""

    fine: list[Point]
    cum: list[float]  # cumulative pixel length at each fine point
    closed: bool

    @property
    def length_px(self) -> float:
        return self.cum[-1]

    @classmethod
    def build(cls, path_px: list[Point], *, closed: bool) -> "TrackGeometry":
        fine = densify(path_px, closed=closed)
        if closed and fine[0] != fine[-1]:
            fine = [*fine, fine[0]]
        cum = [0.0]
        for a, b in zip(fine, fine[1:], strict=False):
            cum.append(cum[-1] + math.dist(a, b))
        return cls(fine=fine, cum=cum, closed=closed)

    # --- s <-> pixel -----------------------------------------------------------
    def point_at(self, s: float) -> Point:
        """Pixel coordinate at normalised arc-length s (wraps if closed)."""
        s = s % 1.0 if self.closed else max(0.0, min(1.0, s))
        target = s * self.length_px
        lo, hi = 0, len(self.cum) - 1
        while lo < hi:
            mid = (lo + hi) // 2
            if self.cum[mid] < target:
                lo = mid + 1
            else:
                hi = mid
        i = max(1, lo)
        seg = self.cum[i] - self.cum[i - 1] or 1.0
        f = (target - self.cum[i - 1]) / seg
        (x0, y0), (x1, y1) = self.fine[i - 1], self.fine[i]
        return (x0 + f * (x1 - x0), y0 + f * (y1 - y0))

    def project(self, px: Point) -> float:
        """Nearest normalised arc-length s to an arbitrary pixel point."""
        best_s, best_d = 0.0, math.inf
        for i in range(1, len(self.fine)):
            (x0, y0), (x1, y1) = self.fine[i - 1], self.fine[i]
            dx, dy = x1 - x0, y1 - y0
            seg2 = dx * dx + dy * dy or 1.0
            t = max(0.0, min(1.0, ((px[0] - x0) * dx + (px[1] - y0) * dy) / seg2))
            cx, cy = x0 + t * dx, y0 + t * dy
            d = math.hypot(px[0] - cx, px[1] - cy)
            if d < best_d:
                best_d = d
                best_s = (self.cum[i - 1] + t * math.hypot(dx, dy)) / self.length_px
        return best_s

    # --- loop distance -------------------------------------------------------------
    def forward_gap(self, s_from: float, s_to: float) -> float:
        """Normalised distance travelling forward along the belt from s_from to s_to."""
        d = (s_to - s_from) % 1.0 if self.closed else (s_to - s_from)
        return d


def build_segments(counting_s: list[tuple[str, float]]) -> list[tuple[str, str, float, float]]:
    """Order counting lines around the loop -> segments (upstream_id, downstream_id, s0, s1)."""
    ordered = sorted(counting_s, key=lambda kv: kv[1])
    segs: list[tuple[str, str, float, float]] = []
    for i in range(len(ordered)):
        a_id, a_s = ordered[i]
        b_id, b_s = ordered[(i + 1) % len(ordered)]
        segs.append((a_id, b_id, a_s, b_s))
    return segs
