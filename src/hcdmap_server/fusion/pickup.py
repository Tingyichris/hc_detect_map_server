"""Turn an overdue counting-line crossing into an attributed pickup.

Attribution priority (see root CLAUDE.md "Pickup detection"):
  1. offbelt_confirmed  - the plate's last camera reported exit_side="offbelt" -> that table, high conf.
  2. conservation       - crossed upstream line, never crossed downstream line. Attribute to the
                          table(s) in the segment. With 1:1 camera:table and one table per
                          sub-segment, an intermediate camera that is ONLINE refines which table
                          (crossed it -> downstream table; didn't -> upstream table).
  3. occluded_suspect   - the intermediate camera is degraded/occluded AND plates are going
                          missing in its neighbourhood -> attribute to ITS table, lower conf,
                          raise a `camera_occluded` anomaly + staff alert.
A crossing inside a `role: source` camera's segment that goes missing is a `kitchen_removal`,
not billable.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..map.model import TrackMap
from .checkpoints import UnmatchedCrossing


@dataclass(slots=True)
class Pickup:
    gid: str
    plate_class: str
    ts: float
    table_id: str | None
    camera_id: str
    confidence: float
    method: str                    # offbelt_confirmed | conservation | occluded_suspect
    segment: tuple[str, str]
    kitchen_removal: bool
    integrity: str                 # ok | estimated | degraded


def detect(
    uc: UnmatchedCrossing,
    track_map: TrackMap,
    *,
    camera_states: dict[str, str],
    offbelt_gids: set[str],
    coldstart_done: bool,
) -> Pickup | None:
    up_id, dn_id = uc.segment
    seg = next((s for s in track_map.segments if (s.upstream_id, s.downstream_id) == uc.segment), None)
    if seg is None:
        return None
    if seg.handoff_only:
        return None

    up_cam = track_map.cameras[up_id]
    kitchen_removal = up_cam.role == "source"
    integrity = "ok" if coldstart_done else "estimated"

    if uc.crossing.gid in offbelt_gids:
        return Pickup(
            gid=uc.crossing.gid, plate_class=uc.crossing.plate_class, ts=uc.crossing.ts,
            table_id=up_cam.table_id, camera_id=up_id, confidence=0.9,
            method="offbelt_confirmed", segment=uc.segment,
            kitchen_removal=kitchen_removal, integrity=integrity,
        )

    tables = seg.tables or ([up_cam.table_id] if up_cam.table_id else [])
    occluded = [c for c in (up_id, dn_id) if camera_states.get(c) in ("degraded", "offline")]

    if occluded and tables:
        suspect_cam = occluded[0]
        return Pickup(
            gid=uc.crossing.gid, plate_class=uc.crossing.plate_class, ts=uc.crossing.ts,
            table_id=track_map.cameras[suspect_cam].table_id or tables[0],
            camera_id=suspect_cam, confidence=0.45,
            method="occluded_suspect", segment=uc.segment,
            kitchen_removal=kitchen_removal, integrity="degraded",
        )

    if not tables:
        return None
    conf = 0.75 if len(tables) == 1 else 0.5
    return Pickup(
        gid=uc.crossing.gid, plate_class=uc.crossing.plate_class, ts=uc.crossing.ts,
        table_id=tables[0], camera_id=up_id, confidence=conf,
        method="conservation", segment=uc.segment,
        kitchen_removal=kitchen_removal, integrity=integrity,
    )
