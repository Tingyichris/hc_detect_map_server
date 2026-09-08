"""TrackState: the snapshot-serialisable view of what's on the belt + per-table tallies."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class TableTally:
    table_id: str
    counts_by_class: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    gids: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(self.counts_by_class.values())

    def add(self, plate_class: str, gid: str) -> None:
        self.counts_by_class[plate_class] += 1
        self.gids.append(gid)

    def remove(self, plate_class: str, gid: str) -> None:
        if self.counts_by_class.get(plate_class, 0) > 0:
            self.counts_by_class[plate_class] -= 1
        if gid in self.gids:
            self.gids.remove(gid)


@dataclass(slots=True)
class TrackState:
    tallies: dict[str, TableTally] = field(default_factory=dict)
    integrity: str = "estimated"     # estimated (cold start) | ok | degraded
    emitted_pickup_gids: set[str] = field(default_factory=set)

    def tally(self, table_id: str) -> TableTally:
        return self.tallies.setdefault(table_id, TableTally(table_id))

    def to_dict(self) -> dict[str, Any]:
        return {
            "integrity": self.integrity,
            "emitted_pickup_gids": sorted(self.emitted_pickup_gids),
            "tallies": {
                t: {"counts_by_class": dict(v.counts_by_class), "gids": v.gids}
                for t, v in self.tallies.items()
            },
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TrackState":
        st = cls(integrity=d.get("integrity", "estimated"))
        st.emitted_pickup_gids = set(d.get("emitted_pickup_gids", []))
        for t, v in d.get("tallies", {}).items():
            tt = st.tally(t)
            tt.counts_by_class = defaultdict(int, v.get("counts_by_class", {}))
            tt.gids = list(v.get("gids", []))
        return st
