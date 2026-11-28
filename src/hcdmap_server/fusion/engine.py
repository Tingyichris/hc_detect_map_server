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
    crossing_counts: dict[str, int] = field(default_factory=dict)
    color: str = "#8892a6"
    motion_start_s: float = 0.0
    motion_target_s: float = 0.0
    motion_started_at: float = 0.0
    motion_duration_s: float = 1.0
    pending_until: float = 0.0


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
        # Stable identity shared by upstream and downstream detector events.
        self._handoff_bindings: dict[str, str] = {}
        self.offbelt_gids: set[str] = set()

    def reset_live_state(self) -> None:
        """Clear ephemeral tracking state while preserving map and learned travel times."""
        self.plates.clear()
        self._binding.clear()
        self._handoffs.clear()
        self._unmatched_entries.clear()
        self._handoff_bindings.clear()
        self.offbelt_gids.clear()
        self.matcher = CheckpointMatcher(
            self.travel, self.cfg.fusion.match_window_s
        )

    def update_map(self, track_map: TrackMap) -> None:
        """Hot-reload map geometry without discarding live plate identity/state."""
        travel_snapshot = self.travel.snapshot()
        old_logs = self.matcher.logs
        valid_edges = {
            (segment.upstream_id, segment.downstream_id)
            for segment in track_map.segments
        }

        travel = TravelTimeModel()
        travel.seed_from_map(
            [
                (
                    segment.upstream_id,
                    segment.downstream_id,
                    track_map.geom.forward_gap(segment.s0, segment.s1),
                )
                for segment in track_map.segments
            ],
            track_map.approx_loop_seconds,
        )
        travel.restore({
            key: samples
            for key, samples in travel_snapshot.items()
            if tuple(key.split(">", 1)) in valid_edges
        })

        matcher = CheckpointMatcher(travel, self.cfg.fusion.match_window_s)
        matcher.logs = {
            camera_id: log
            for camera_id, log in old_logs.items()
            if camera_id in track_map.cameras
        }

        self.map = track_map
        self.travel = travel
        self.matcher = matcher

        valid_cameras = set(track_map.cameras)
        self._binding = {
            key: gid for key, gid in self._binding.items()
            if key[0] in valid_cameras and gid in self.plates
        }
        self._handoffs = {
            camera_id: [gid for gid in gids if gid in self.plates]
            for camera_id, gids in self._handoffs.items()
            if camera_id in valid_cameras
        }
        self._unmatched_entries = {
            camera_id: [gid for gid in gids if gid in self.plates]
            for camera_id, gids in self._unmatched_entries.items()
            if camera_id in valid_cameras
        }
        self._handoff_bindings = {
            handoff_id: gid
            for handoff_id, gid in self._handoff_bindings.items()
            if gid in self.plates
        }
        for plate in self.plates.values():
            plate.s %= 1.0
            plate.seen_by = {
                camera_id: value
                for camera_id, value in plate.seen_by.items()
                if camera_id in valid_cameras
            }

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
        elif ev.event == "update":
            self._on_update(ev)
        elif ev.event == "catch_suspected":
            self._on_catch_suspected(ev)
        elif ev.event == "catch_revoked":
            self._on_catch_revoked(ev)
        elif ev.event == "catch":
            self._on_catch(ev, cam)
        else:
            self._on_exit(ev, cam)

    def _next_camera(self, camera_id: str) -> str | None:
        for segment in self.map.segments:
            if segment.upstream_id == camera_id:
                return segment.downstream_id
        return None

    def _start_motion(
        self, plate: Plate, start_s: float, target_s: float, now: float,
        duration_s: float | None = None,
    ) -> None:
        gap = self.map.geom.forward_gap(start_s, target_s)
        loop_seconds = self.travel.loop_seconds_estimate() or self.map.approx_loop_seconds or 120.0
        plate.s = start_s
        plate.motion_start_s = start_s
        plate.motion_target_s = target_s
        plate.motion_started_at = now
        plate.motion_duration_s = max(
            0.5, duration_s if duration_s is not None else loop_seconds * gap
        )

    def _camera_motion_duration(self, camera_id: str) -> float | None:
        downstream = self._next_camera(camera_id)
        if downstream is None:
            return None
        return self.travel.get(camera_id, downstream).estimate()

    def _upstream_camera(self, camera_id: str) -> str | None:
        for segment in self.map.segments:
            if segment.downstream_id == camera_id:
                return segment.upstream_id
        return None

    def _on_entry(self, ev: PlateEvent, cam) -> None:
        waiting = self._handoffs.setdefault(cam.id, [])
        # A shared handoff ID is authoritative. It lets the downstream camera
        # acquire the existing gid immediately, without waiting for colour/time
        # heuristics or for the upstream exit event to arrive first.
        gid = self._handoff_bindings.get(ev.handoff_id or "")
        if gid is not None and gid in self.plates:
            while gid in waiting:
                waiting.remove(gid)
            # A shared identity already resolved this entry.  Remove stale
            # provisional references from earlier out-of-order handling so a
            # later exit cannot accidentally merge the plate with itself.
            for unmatched in self._unmatched_entries.values():
                while gid in unmatched:
                    unmatched.remove(gid)
            plate = self.plates[gid]
            plate.s = cam.span[0]
            plate.last_ts = ev.ts
            plate.status = "tracked"
            plate.pending_until = 0.0
            plate.color = ev.color
            plate.plate_class = ev.plate_class
            plate.seen_by[ev.camera_id] = (ev.plate_id, ev.ts)
            self._binding[(ev.camera_id, ev.plate_id)] = gid
            self._start_motion(
                plate, cam.span[0], cam.span[1], ev.ts,
                self._camera_motion_duration(cam.id),
            )
            return
        upstream_id = self._upstream_camera(cam.id)
        candidates: list[tuple[float, int, str]] = []
        if upstream_id is not None:
            estimator = self.travel.get(upstream_id, cam.id)
            lo, hi = estimator.match_window(self.cfg.fusion.match_window_s)
            for order, candidate in enumerate(waiting):
                if candidate not in self.plates:
                    continue
                plate = self.plates[candidate]
                # The travel model is checkpoint-entry to checkpoint-entry. `last_ts`
                # is overwritten by the upstream exit event, so using it here turns a
                # valid multi-second trip into a near-zero trip and creates a duplicate
                # gid at the downstream camera. `motion_started_at` remains the entry
                # timestamp for the segment currently being traversed.
                travel_s = ev.ts - plate.motion_started_at
                if plate.plate_class != ev.plate_class or not lo <= travel_s <= hi:
                    continue
                candidates.append((abs(travel_s - estimator.estimate()), order, candidate))
        gid = min(candidates)[2] if candidates else None
        if gid is not None:
            waiting.remove(gid)
            plate = self.plates[gid]
            plate.s = cam.span[0]
            plate.last_ts = ev.ts
            plate.status = "tracked"
            plate.pending_until = 0.0
            plate.color = ev.color
            plate.seen_by[ev.camera_id] = (ev.plate_id, ev.ts)
            self._binding[(ev.camera_id, ev.plate_id)] = gid
            if ev.handoff_id:
                self._handoff_bindings[ev.handoff_id] = gid
            self._start_motion(
                plate, cam.span[0], cam.span[1], ev.ts,
                self._camera_motion_duration(cam.id),
            )
            return

        gid = self._new_gid()
        upstream_id = self._upstream_camera(cam.id)
        pending_until = 0.0
        if upstream_id is not None:
            travel_seconds = self.travel.get(upstream_id, cam.id).estimate()
            pending_until = ev.ts + max(
                self.cfg.fusion.match_window_s * 5,
                travel_seconds + self.cfg.fusion.match_window_s,
            )
        plate = Plate(gid=gid, plate_class=ev.plate_class, s=cam.span[0], last_ts=ev.ts,
                      color=ev.color, seen_by={ev.camera_id: (ev.plate_id, ev.ts)},
                      pending_until=pending_until)
        self.plates[gid] = plate
        self._start_motion(
            plate, cam.span[0], cam.span[1], ev.ts,
            self._camera_motion_duration(cam.id),
        )
        self._binding[(ev.camera_id, ev.plate_id)] = gid
        if ev.handoff_id:
            self._handoff_bindings[ev.handoff_id] = gid
        self._unmatched_entries.setdefault(cam.id, []).append(gid)

    def _on_update(self, ev: PlateEvent) -> None:
        gid = self._binding.get((ev.camera_id, ev.plate_id))
        if gid is None and ev.handoff_id:
            gid = self._handoff_bindings.get(ev.handoff_id)
        if gid in self.plates:
            if ev.handoff_id:
                self._handoff_bindings[ev.handoff_id] = gid
            plate = self.plates[gid]
            plate.plate_class = ev.plate_class
            plate.color = ev.color
            plate.last_ts = max(plate.last_ts, ev.ts)

    def _on_exit(self, ev: PlateEvent, cam) -> None:
        gid = self._binding.pop((ev.camera_id, ev.plate_id), None)
        if gid is None or gid not in self.plates:
            return
        p = self.plates[gid]
        p.seen_by.pop(ev.camera_id, None)
        p.last_ts = ev.ts
        p.color = ev.color

        # Record the crossing at this camera's counting line (canonical checkpoint).
        p.crossing_counts[cam.id] = p.crossing_counts.get(cam.id, 0) + 1
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
        # A fast downstream entry can arrive before this upstream exit. In that
        # ordering the downstream side has already created a provisional gid.
        # Reconcile it with the same entry-to-entry travel window used above;
        # comparing the two events directly to the generic 2 s window loses the
        # identity whenever cameras are more than 2 s apart.
        estimator = self.travel.get(cam.id, downstream)
        lo, hi = estimator.match_window(self.cfg.fusion.match_window_s)
        late_candidates: list[tuple[float, int, str]] = []
        for order, candidate in enumerate(unmatched):
            if candidate == gid or candidate not in self.plates:
                continue
            late_plate = self.plates[candidate]
            travel_s = late_plate.motion_started_at - p.motion_started_at
            if late_plate.plate_class != p.plate_class or not lo <= travel_s <= hi:
                continue
            late_candidates.append(
                (abs(travel_s - estimator.estimate()), order, candidate)
            )
        late_gid = min(late_candidates)[2] if late_candidates else None
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
        p.crossing_counts[cam.id] = p.crossing_counts.get(cam.id, 0) + 1
        self.matcher.log_for(cam.id).record(
            Crossing(cam.id, gid, p.plate_class, ev.ts)
        )

    def _on_catch_suspected(self, ev: PlateEvent) -> None:
        gid = self._binding.get((ev.camera_id, ev.plate_id))
        if gid in self.plates:
            self.plates[gid].status = "suspected"

    def _on_catch_revoked(self, ev: PlateEvent) -> None:
        gid = self._binding.get((ev.camera_id, ev.plate_id))
        if gid in self.plates:
            plate = self.plates[gid]
            plate.status = "tracked" if plate.seen_by else "predicted"

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
        for handoff_id, bound_gid in list(self._handoff_bindings.items()):
            if bound_gid == gid:
                del self._handoff_bindings[handoff_id]

    # -- periodic tick --------------------------------------------------------------
    def tick(self, now: float) -> list[UnmatchedCrossing]:
        """Advance predicted plates, run checkpoint alignment, expire stale plates."""
        for plate in self.plates.values():
            if plate.status == "lost":
                continue
            if plate.pending_until and now >= plate.pending_until:
                plate.pending_until = 0.0
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
