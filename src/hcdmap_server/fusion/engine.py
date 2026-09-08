"""Global plate list: assign gids, bind track_ids across overlapping cameras, fuse positions.

STATUS: skeleton. The data model and the public surface are settled; the matching bodies are
marked TODO with the algorithm from shared/ + root CLAUDE.md.
"""
from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass, field

from ..config import Config
from ..map.model import TrackMap
from ..mqtt.schema import PlateEvent
from .checkpoints import CheckpointMatcher, Crossing
from .travel_time import TravelTimeModel

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Plate:
    gid: str
    plate_class: str
    s: float                          # normalised arc-length
    last_ts: float
    status: str = "tracked"           # tracked | predicted | lost
    seen_by: dict[str, tuple[int, float]] = field(default_factory=dict)  # cam -> (track_id, ts)
    confidence: float = 1.0
    crossed_lines: set[str] = field(default_factory=set)


class FusionEngine:
    def __init__(self, cfg: Config, track_map: TrackMap) -> None:
        self.cfg = cfg
        self.map = track_map
        self.travel = TravelTimeModel()
        self.travel.seed_from_map(
            [(s.upstream_id, s.downstream_id, self.map.geom.forward_gap(s.s0, s.s1))
             for s in self.map.segments],
            self.map.approx_loop_seconds,
        )
        self.matcher = CheckpointMatcher(self.travel, cfg.fusion.match_window_s)
        self.plates: dict[str, Plate] = {}
        self._gid_counter = itertools.count(1)
        # (camera_id, track_id) -> gid  binding table, cleared when a track exits + reacquire window passes
        self._binding: dict[tuple[str, int], str] = {}

    def _new_gid(self) -> str:
        return f"p_{next(self._gid_counter):06d}"

    # -- ingest -----------------------------------------------------------------
    def on_event(self, ev: PlateEvent) -> None:
        cam = self.map.cameras.get(ev.camera_id)
        if cam is None:
            log.warning("event from unknown camera %s (not in map v%d)", ev.camera_id, self.map.version)
            return
        s = self._event_to_s(ev, cam)

        if ev.event == "enter":
            self._on_enter(ev, cam, s)
        elif ev.event == "update":
            self._on_update(ev, s)
        else:
            self._on_exit(ev, cam, s)

    def _event_to_s(self, ev: PlateEvent, cam) -> float:
        """Map pos_norm (0..1 within the camera span) onto the global loop arc-length."""
        span0, span1 = cam.span
        pos = ev.pos_norm if ev.pos_norm is not None else 0.5
        return (span0 + self.map.geom.forward_gap(span0, span1) * pos) % 1.0

    def _on_enter(self, ev: PlateEvent, cam, s: float) -> None:
        # TODO: search self.plates for a gid whose predicted s (last_s + travel over dt) is
        # within cfg.fusion.match_radius_s of `s` AND same class -> bind (overlap / handoff).
        # TODO: re-acquire hysteresis: a gid that went lost < reacquire_window_s ago near `s`.
        # Fallthrough: brand new plate.
        gid = self._new_gid()
        self.plates[gid] = Plate(gid=gid, plate_class=ev.plate_class, s=s, last_ts=ev.ts,
                                 confidence=ev.conf, seen_by={ev.camera_id: (ev.track_id, ev.ts)})
        self._binding[(ev.camera_id, ev.track_id)] = gid

    def _on_update(self, ev: PlateEvent, s: float) -> None:
        gid = self._binding.get((ev.camera_id, ev.track_id))
        if gid is None or gid not in self.plates:
            return
        p = self.plates[gid]
        # TODO: position fusion when >1 camera reports this gid (weight by conf / view-centre distance)
        p.s = s
        p.last_ts = ev.ts
        p.status = "tracked"
        p.seen_by[ev.camera_id] = (ev.track_id, ev.ts)

    def _on_exit(self, ev: PlateEvent, cam, s: float) -> None:
        gid = self._binding.pop((ev.camera_id, ev.track_id), None)
        if gid is None or gid not in self.plates:
            return
        p = self.plates[gid]
        p.seen_by.pop(ev.camera_id, None)

        # Record the crossing at this camera's counting line (canonical checkpoint).
        if cam.id not in p.crossed_lines:
            p.crossed_lines.add(cam.id)
            self.matcher.log_for(cam.id).record(
                Crossing(cam.id, gid, p.plate_class, ev.ts)
            )

        if ev.exit_side == "offbelt":
            # High-confidence pickup; pickup.py turns this into an attributed event.
            p.status = "lost"
        elif not p.seen_by:
            # Left this camera's view; may be handed to the next camera or be in a gap.
            p.status = "predicted"

    # -- periodic tick --------------------------------------------------------------
    def tick(self, now: float) -> None:
        """Advance predicted plates, run checkpoint alignment, expire stale plates."""
        # TODO: dead-reckon `predicted` plates: s += forward distance over (now - last_ts)
        #       using travel-time estimate for the segment they're in.
        for seg in self.map.segments:
            overdue = self.matcher.align_segment(seg.upstream_id, seg.downstream_id, now)
            # overdue -> hand to pickup.detect() (wired in __main__)
            _ = overdue
        # TODO: expire plates with status == "lost" once emitted; drop plates unseen for >> loop time.
