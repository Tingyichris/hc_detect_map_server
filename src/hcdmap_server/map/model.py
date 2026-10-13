"""Typed map model + loader. Fetched from backend; hot-reloaded on the config/map signal.

Document shape: docs/map-schema.md.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

from .geometry import Point, TrackGeometry, build_segments

CameraRole = Literal["table", "source"]


@dataclass(slots=True)
class Camera:
    id: str
    role: CameraRole
    counting_s: float
    span: tuple[float, float]              # (s_start, s_end) along the loop
    table_id: str | None = None
    pickup_zone: tuple[float, float] | None = None


@dataclass(slots=True)
class Segment:
    upstream_id: str
    downstream_id: str
    s0: float
    s1: float
    tables: list[str] = field(default_factory=list)   # role: table cameras whose span falls here
    has_overlap: bool = False                          # adjacent spans overlap -> simultaneous de-dup
    gap_len: float = 0.0                               # >0 -> dead-reckon across this segment


@dataclass(slots=True)
class TrackMap:
    version: int
    store_id: str
    geom: TrackGeometry
    cameras: dict[str, Camera]
    segments: list[Segment]
    approx_loop_seconds: float | None
    floorplan: dict[str, Any]

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TrackMap":
        track = d["track"]
        path_px: list[Point] = [tuple(p) for p in track["path_px"]]
        geom = TrackGeometry.build(path_px, closed=track.get("closed", True))

        cameras: dict[str, Camera] = {}
        for c in d["cameras"]:
            span_px = c["view_span_px"]
            span = (geom.project(tuple(span_px[0])), geom.project(tuple(span_px[1])))
            # Camera markers are the canonical checkpoints. Older map versions may contain
            # hand-drawn spans/counting points that no longer line up with the marker.
            checkpoint_px = c.get("marker_px") or c["counting_point_px"]
            zone = None
            if c.get("pickup_zone_px"):
                z = c["pickup_zone_px"]
                zone = (geom.project(tuple(z[0])), geom.project(tuple(z[1])))
            cameras[c["id"]] = Camera(
                id=c["id"],
                role=c.get("role", "table"),
                counting_s=geom.project(tuple(checkpoint_px)),
                span=span,
                table_id=c.get("table_id"),
                pickup_zone=zone,
            )

        raw_segs = build_segments([(c.id, c.counting_s) for c in cameras.values()])
        segments: list[Segment] = []
        for up_id, dn_id, s0, s1 in raw_segs:
            up, dn = cameras[up_id], cameras[dn_id]
            # A view is exactly the conveyor section between adjacent camera checkpoints.
            # Ignore legacy hand-drawn view_span_px values when running fusion.
            up.span = (s0, s1)
            overlap = False
            gap = 0.0
            tables = [
                c.table_id
                for c in cameras.values()
                if c.role == "table"
                and c.table_id
                and geom.forward_gap(s0, c.counting_s) <= geom.forward_gap(s0, s1)
            ]
            segments.append(
                Segment(up_id, dn_id, s0, s1, tables=tables, has_overlap=overlap, gap_len=gap)
            )

        return cls(
            version=d["version"],
            store_id=d["store_id"],
            geom=geom,
            cameras=cameras,
            segments=segments,
            approx_loop_seconds=track.get("approx_loop_seconds"),
            floorplan=d.get("floorplan", {}),
        )


async def fetch_current(backend_url: str, token: str) -> TrackMap:
    async with httpx.AsyncClient(base_url=backend_url, timeout=10) as client:
        r = await client.get("/api/maps/current", headers={"Authorization": f"Bearer {token}"})
        r.raise_for_status()
        return TrackMap.from_dict(r.json())
