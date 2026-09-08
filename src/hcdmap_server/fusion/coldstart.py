"""Cold-start inventory: on boot, plates are already on the belt with no history.

Observe one full loop (max segment travel-time sum, or `approx_loop_seconds`, plus margin)
before trusting counts. During that window every pickup/count is tagged `integrity="estimated"`.
"""
from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass(slots=True)
class ColdStart:
    started_at: float
    loop_seconds: float
    margin: float = 1.25

    @classmethod
    def begin(cls, loop_seconds: float) -> "ColdStart":
        return cls(started_at=time.time(), loop_seconds=max(loop_seconds, 30.0))

    def done(self, now: float | None = None) -> bool:
        now = now or time.time()
        return now - self.started_at >= self.loop_seconds * self.margin
