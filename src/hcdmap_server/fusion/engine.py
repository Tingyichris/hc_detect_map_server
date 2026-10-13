"""Global plate list: assign gids, bind plate IDs across overlapping cameras, fuse positions.

The current implementation records crossings and exposes overdue pickup candidates to the
main loop. Advanced matching, re-acquire and dead-reckoning bodies are still marked TODO.
"""
from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass, field

from ..config import Config
from ..map.model import TrackMap
from ..mqtt.schema import PlateEvent
from .checkpoints import CheckpointMatcher, Crossing, UnmatchedCrossing
from .travel_time import TravelTimeModel

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Plate:
    gid: str
    plate_class: str
    s: float                          # normalised arc-length
    last_ts: float
    status: str = "tracked"           # tracked | predicted | lost
    seen_by: dict[str, tuple[int, float]] = field(default_factory=dict)  # cam -> (plate_id, ts)
    confidence: float = 1.0
    crossed_lines: set[str] = field(default_factory=set)
    color: str = "#8892a6"
    motion_start_s: float = 0.0
    motion_target_s: float = 0.0
    motion_started_at: float = 0.0
    motion_duration_s: float = 1.0


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
        # (camera_id, plate_id) -> gid binding table, cleared when a plate exits.
        self._binding: dict[tuple[str, int], str] = {}
        self._handoffs: dict[str, list[str]] = {}
        self._unmatched_entries: dict[str, list[str]] = {}
        self.offbelt_gids: set[str] = set()

    def _new_gid(self) -> str:
        return f"p_{next(self._gid_counter):06d}"

    # -- ingest -----------------------------------------------------------------
    def on_event(self, ev: PlateEvent) -> None:
        cam = self.map.cameras.get(ev.camera_id)
        if cam is None:
            log.warning("event from unknown camera %s (not in map v%d)", ev.camera_id, self.map.version)
            return
        if ev.event == "entry":
            self._on_entry(ev, cam)
        elif ev.event == "catch":
            self._on_catch(ev, cam)
        else:
            self._on_exit(ev, cam)

    def _next_camera(self, camera_id: str) -> str | None:
        for segment in self.map.segments:
            if segment.upstream_id == camera_id:
                return segment.downstream_id
        return None

    def _start_motion(self, plate: Plate, start_s: float, target_s: float, now: float) -> None:
        gap = self.map.geom.forward_gap(start_s, target_s)
        loop_seconds = self.travel.loop_seconds_estimate() or self.map.approx_loop_seconds or 120.0
        plate.s = start_s
        plate.motion_start_s = start_s
        plate.motion_target_s = target_s
        plate.motion_started_at = now
        plate.motion_duration_s = max(0.5, loop_seconds * gap)

    def _on_entry(self, ev: PlateEvent, cam) -> None:
        waiting = self._handoffs.setdefault(cam.id, [])
        gid = next(
            (
                candidate
                for candidate in waiting
                if candidate in self.plates
                and self.plates[candidate].plate_class == ev.plate_class
            ),
            None,
        )
        if gid is not None:
            waiting.remove(gid)
            plate = self.plates[gid]
            plate.s = cam.span[0]
            plate.last_ts = ev.ts
            plate.status = "tracked"
            plate.color = ev.color
            plate.seen_by[ev.camera_id] = (ev.plate_id, ev.ts)
            self._binding[(ev.camera_id, ev.plate_id)] = gid
            self._start_motion(plate, cam.span[0], cam.span[1], ev.ts)
            return

        gid = self._new_gid()
        plate = Plate(gid=gid, plate_class=ev.plate_class, s=cam.span[0], last_ts=ev.ts,
                      color=ev.color, seen_by={ev.camera_id: (ev.plate_id, ev.ts)})
        self.plates[gid] = plate
        self._start_motion(plate, cam.span[0], cam.span[1], ev.ts)
        self._binding[(ev.camera_id, ev.plate_id)] = gid
        self._unmatched_entries.setdefault(cam.id, []).append(gid)

    def _on_exit(self, ev: PlateEvent, cam) -> None:
        gid = self._binding.pop((ev.camera_id, ev.plate_id), None)
        if gid is None or gid not in self.plates:
            return
        p = self.plates[gid]
        p.seen_by.pop(ev.camera_id, None)
        p.last_ts = ev.ts
        p.color = ev.color

        # Record the crossing at this camera's counting line (canonical checkpoint).
        if cam.id not in p.crossed_lines:
            p.crossed_lines.add(cam.id)
            self.matcher.log_for(cam.id).record(
                Crossing(cam.id, gid, p.plate_class, ev.ts)
            )

        if not p.seen_by:
            p.status = "predicted"

        downstream = self._next_camera(cam.id)
        if downstream is None:
            return
        # Entry already started interpolation across the current camera-to-camera segment.
        # Exit only means the plate is no longer directly observed; snapping it to span[1]
        # here made it jump prematurely to the next checkpoint and appear to stop halfway.
        unmatched = self._unmatched_entries.setdefault(downstream, [])
        late_gid = next(
            (
                candidate
                for candidate in unmatched
                if candidate in self.plates
                and self.plates[candidate].plate_class == p.plate_class
                and abs(self.plates[candidate].last_ts - ev.ts)
                <= self.cfg.fusion.match_window_s
            ),
            None,
        )
        if late_gid is not None:
            unmatched.remove(late_gid)
            late = self.plates.pop(late_gid)
            for camera_id, (plate_id, seen_ts) in late.seen_by.items():
                self._binding[(camera_id, plate_id)] = gid
                p.seen_by[camera_id] = (plate_id, seen_ts)
            p.s = late.s
            p.last_ts = max(p.last_ts, late.last_ts)
            p.status = "tracked"
            p.color = late.color
            p.motion_start_s = late.motion_start_s
            p.motion_target_s = late.motion_target_s
            p.motion_started_at = late.motion_started_at
            p.motion_duration_s = late.motion_duration_s
        else:
            self._handoffs.setdefault(downstream, []).append(gid)

    def _on_catch(self, ev: PlateEvent, cam) -> None:
        gid = self._binding.pop((ev.camera_id, ev.plate_id), None)
        if gid is None or gid not in self.plates:
            return
        p = self.plates[gid]
        p.seen_by.pop(ev.camera_id, None)
        p.s = cam.counting_s
        p.last_ts = ev.ts
        p.color = ev.color
        p.status = "lost"
        self.offbelt_gids.add(gid)
        if cam.id not in p.crossed_lines:
            p.crossed_lines.add(cam.id)
            self.matcher.log_for(cam.id).record(
                Crossing(cam.id, gid, p.plate_class, ev.ts)
            )

    def mark_pickup_emitted(self, gid: str) -> None:
        """Remove a confirmed pickup from the live belt inventory."""
        plate = self.plates.get(gid)
        if plate is None:
            return
        plate.status = "lost"
        for key, bound_gid in list(self._binding.items()):
            if bound_gid == gid:
                del self._binding[key]
        for waiting in (*self._handoffs.values(), *self._unmatched_entries.values()):
            while gid in waiting:
                waiting.remove(gid)

    # -- periodic tick --------------------------------------------------------------
    def tick(self, now: float) -> list[UnmatchedCrossing]:
        """Advance predicted plates, run checkpoint alignment, expire stale plates."""
        for plate in self.plates.values():
            if plate.status == "lost":
                continue
            progress = min(
                1.0,
                max(0.0, (now - plate.motion_started_at) / plate.motion_duration_s),
            )
            gap = self.map.geom.forward_gap(plate.motion_start_s, plate.motion_target_s)
            plate.s = (plate.motion_start_s + gap * progress) % 1.0
        overdue: list[UnmatchedCrossing] = []
        for seg in self.map.segments:
            for uc in self.matcher.align_segment(seg.upstream_id, seg.downstream_id, now):
                # A missing downstream crossing is a durable decision candidate. Mark it handled
                # here so the 0.5s tick loop does not submit the same pickup repeatedly.
                uc.crossing.matched_downstream = True
                overdue.append(uc)
        # TODO: expire plates with status == "lost" once emitted; drop plates unseen for >> loop time.
        return overdue
