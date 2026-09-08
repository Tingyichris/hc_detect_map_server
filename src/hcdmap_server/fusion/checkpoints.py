"""Counting-line crossing logs + FIFO sequence alignment between adjacent lines.

The belt is a FIFO conveyor with no overtaking, so the ordered stream of crossings at
counting line A, shifted by the segment travel time, should equal the stream at line B.
Plates present in A's stream but absent from B's are the ones that left the belt in that
segment (a pickup, or a kitchen removal at a `source` camera).
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from .travel_time import TravelTimeModel


@dataclass(slots=True)
class Crossing:
    camera_id: str
    gid: str
    plate_class: str
    ts: float
    matched_downstream: bool = False


@dataclass(slots=True)
class CheckpointLog:
    camera_id: str
    crossings: deque[Crossing] = field(default_factory=lambda: deque(maxlen=2000))

    def record(self, c: Crossing) -> None:
        self.crossings.append(c)


@dataclass(slots=True)
class UnmatchedCrossing:
    """A plate that crossed the upstream line but never reached the downstream line."""

    segment: tuple[str, str]
    crossing: Crossing
    expected_by_ts: float


class CheckpointMatcher:
    def __init__(self, travel: TravelTimeModel, base_tolerance_s: float) -> None:
        self.travel = travel
        self.base_tolerance_s = base_tolerance_s
        self.logs: dict[str, CheckpointLog] = {}

    def log_for(self, camera_id: str) -> CheckpointLog:
        return self.logs.setdefault(camera_id, CheckpointLog(camera_id))

    def align_segment(
        self, upstream_id: str, downstream_id: str, now: float
    ) -> list[UnmatchedCrossing]:
        """Greedy FIFO match of upstream crossings against downstream crossings.

        Returns upstream crossings that are overdue at the downstream line -> pickup candidates.
        Downstream crossings with no upstream partner -> back-fill (upstream line briefly blind);
        handled by the caller via `engine`.
        """
        up = self.log_for(upstream_id).crossings
        dn = list(self.log_for(downstream_id).crossings)
        est = self.travel.get(upstream_id, downstream_id)
        lo, hi = est.match_window(self.base_tolerance_s)

        dn_used: set[int] = set()
        overdue: list[UnmatchedCrossing] = []
        for uc in up:
            if uc.matched_downstream:
                continue
            expected = uc.ts + est.estimate()
            partner_idx = None
            for j, dc in enumerate(dn):
                if j in dn_used or dc.plate_class != uc.plate_class:
                    continue
                if uc.ts + lo <= dc.ts <= uc.ts + hi:
                    partner_idx = j
                    break
            if partner_idx is not None:
                dn_used.add(partner_idx)
                uc.matched_downstream = True
                est.observe(dn[partner_idx].ts - uc.ts)
            elif now > uc.ts + hi:
                overdue.append(
                    UnmatchedCrossing((upstream_id, downstream_id), uc, uc.ts + hi)
                )
        return overdue
