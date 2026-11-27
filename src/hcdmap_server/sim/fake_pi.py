"""Fake Pis: publish realistic sushi/events + status so the whole backend can be
built and tested with no hardware.

Model: plates are injected at the `source` camera and ride a virtual loop at a configurable
speed. Each camera emits entry followed by exit or catch. A scenario can schedule pickups
(plate removed at a table -> catch at that camera, no downstream events) and occlusions
(a camera goes degraded and stops emitting for a while).

Usage:
    python -m hcdmap_server.sim.fake_pi --scenario normal
    python -m hcdmap_server.sim.fake_pi --scenario occlusion --host localhost --store store-001
"""
from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import random
import time
from dataclasses import dataclass, field
from pathlib import Path

import aiomqtt
import httpx
import yaml

from ..map.model import TrackMap

SCENARIO_DIR = Path(__file__).parent / "scenarios"


@dataclass
class SimCamera:
    id: str
    role: str
    s_start: float
    s_end: float
    table_id: str | None = None


@dataclass
class Scenario:
    store_id: str
    loop_seconds: float
    inject_every_s: tuple[float, float]
    classes: dict[str, float]                     # class -> weight
    cameras: list[SimCamera]
    use_current_map: bool = True
    backend_url: str = "http://127.0.0.1:8000"
    service_token: str = "dev-service-token"
    pickups: list[dict] = field(default_factory=list)     # {after_s, table_id}
    occlusions: list[dict] = field(default_factory=list)  # {camera_id, at_s, duration_s}

    @classmethod
    def load(cls, name: str) -> "Scenario":
        raw = yaml.safe_load((SCENARIO_DIR / f"{name}.yaml").read_text())
        return cls(
            store_id=raw.get("store_id", "store-001"),
            loop_seconds=raw.get("loop_seconds", 108),
            inject_every_s=tuple(raw.get("inject_every_s", [4, 9])),
            classes=raw.get("classes", {"red": 5, "gold": 2, "black": 1, "green": 4}),
            cameras=[SimCamera(**c) for c in raw.get("cameras", [])],
            use_current_map=raw.get("use_current_map", True),
            backend_url=raw.get("backend_url", "http://127.0.0.1:8000"),
            service_token=raw.get("service_token", "dev-service-token"),
            pickups=raw.get("pickups", []),
            occlusions=raw.get("occlusions", []),
        )

    async def bind_current_map(self, backend_url: str | None = None, token: str | None = None) -> bool:
        """Replace scenario cameras with the backend's active map cameras.

        Camera ids must match the active map loaded by the fusion server. Otherwise MQTT
        events are valid but ignored as unknown cameras.
        """
        if not self.use_current_map:
            return False
        if backend_url:
            self.backend_url = backend_url
        if token:
            self.service_token = token
        headers = {"Authorization": f"Bearer {self.service_token}"}
        async with httpx.AsyncClient(base_url=self.backend_url, timeout=10, headers=headers) as client:
            r = await client.get("/api/maps/current")
            r.raise_for_status()
            tm = TrackMap.from_dict(r.json())
        self.store_id = tm.store_id
        self.cameras = [
            SimCamera(
                id=c.id,
                role=c.role,
                s_start=c.span[0],
                s_end=c.span[1],
                table_id=c.table_id,
            )
            for c in tm.cameras.values()
        ]
        return True


class FakePi:
    def __init__(self, sc: Scenario, host: str, port: int) -> None:
        self.sc = sc
        self.host, self.port = host, port
        self._plate_ids = itertools.count(1)
        self._t0 = time.time()
        self._map_lock = asyncio.Lock()
        self._occluded: set[str] = set()

    def _topic(self, kind: str, cam: str) -> str:
        return f"sushi/{kind}/{cam}"

    async def run(self) -> None:
        async with aiomqtt.Client(
            self.host, self.port, protocol=aiomqtt.ProtocolVersion.V311
        ) as client:
            for c in self.sc.cameras:
                await self._status(client, c.id, "online", "ok")
            await asyncio.gather(
                self._map_reload_driver(client),
                self._occlusion_driver(client),
                self._plate_driver(client),
            )

    async def _status(self, client, cam, state, reason) -> None:
        await client.publish(
            self._topic("status", cam),
            json.dumps({"state": state}),
            qos=1, retain=True,
        )

    async def _map_reload_driver(self, client) -> None:
        if not self.sc.use_current_map:
            return
        await client.subscribe(f"sushi/{self.sc.store_id}/config/map", qos=1)
        async for msg in client.messages:
            try:
                payload = json.loads(msg.payload)
            except json.JSONDecodeError:
                payload = {}
            await self._reload_map(client, payload.get("map_version"))

    async def _reload_map(self, client, version: int | None = None) -> None:
        async with self._map_lock:
            old_ids = {c.id for c in self.sc.cameras}
            try:
                await self.sc.bind_current_map()
            except httpx.HTTPError as e:
                print(f"fake_pi: map reload failed: {e}", flush=True)
                return
            new_ids = {c.id for c in self.sc.cameras}
            self._occluded.intersection_update(new_ids)
            for cam_id in old_ids - new_ids:
                await self._status(client, cam_id, "offline", "map_reloaded")
            for cam in self.sc.cameras:
                await self._status(client, cam.id, "online", "ok")
            print(
                f"fake_pi: reloaded map"
                f"{f' v{version}' if version is not None else ''}; cameras={len(new_ids)}",
                flush=True,
            )

    async def _occlusion_driver(self, client) -> None:
        for occ in sorted(self.sc.occlusions, key=lambda o: o["at_s"]):
            await asyncio.sleep(max(0.0, occ["at_s"] - (time.time() - self._t0)))
            cam = occ["camera_id"]
            self._occluded.add(cam)
            await self._status(client, cam, "degraded", "occluded")
            asyncio.create_task(self._clear_occlusion(client, cam, occ["duration_s"]))

    async def _clear_occlusion(self, client, cam, duration) -> None:
        await asyncio.sleep(duration)
        self._occluded.discard(cam)
        await self._status(client, cam, "online", "ok")

    async def _plate_driver(self, client) -> None:
        classes = list(self.sc.classes)
        weights = list(self.sc.classes.values())
        speed = 1.0 / self.sc.loop_seconds  # s-units per second

        while True:
            await asyncio.sleep(random.uniform(*self.sc.inject_every_s))
            cams = sorted(self.sc.cameras, key=lambda c: c.s_start)
            if not cams:
                continue
            plate_class = random.choices(classes, weights)[0]
            born = time.time()
            pickup = next(
                (p for p in self.sc.pickups
                 if abs((born - self._t0) - p["after_s"]) < random.uniform(*self.sc.inject_every_s)),
                None,
            )
            asyncio.create_task(self._ride(client, cams, plate_class, born, speed, pickup))

    async def _ride(self, client, cams, plate_class, born, speed, pickup) -> None:
        for cam in cams:
            enter_delay = self._forward_gap(0.0, cam.s_start) / speed - (time.time() - born)
            if enter_delay > 0:
                await asyncio.sleep(enter_delay)
            if not self._is_active_camera(cam.id):
                continue
            if cam.id in self._occluded:
                continue  # camera blind: emits nothing
            plate_id = next(self._plate_ids)
            span = max(self._forward_gap(cam.s_start, cam.s_end), 1e-4)
            await self._event(client, cam.id, "entry", plate_id, plate_class)
            steps = 4
            for k in range(1, steps):
                await asyncio.sleep((span / speed) / steps)
                if not self._is_active_camera(cam.id):
                    break
            if not self._is_active_camera(cam.id):
                continue
            await asyncio.sleep((span / speed) / steps)
            if pickup and cam.table_id and pickup["table_id"] == cam.table_id:
                await self._event(client, cam.id, "catch", plate_id, plate_class)
                return  # taken here: no further cameras
            await self._event(client, cam.id, "exit", plate_id, plate_class)

    def _forward_gap(self, s_from: float, s_to: float) -> float:
        return (s_to - s_from) % 1.0

    def _is_active_camera(self, cam_id: str) -> bool:
        return any(c.id == cam_id for c in self.sc.cameras)

    async def _event(self, client, cam, kind, plate_id, plate_class) -> None:
        if not self._is_active_camera(cam):
            return
        colors = {
            "red": "#e0564c", "gold": "#e0a94c", "black": "#2f333d",
            "green": "#4cae7a",
        }
        payload = {
            "event": kind, "plate_id": plate_id, "class": plate_class,
            "color": colors.get(plate_class, "#8892a6"),
        }
        await client.publish(self._topic("events", cam), json.dumps(payload), qos=1)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", default="normal")
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=1883)
    ap.add_argument("--backend-url", default=None)
    ap.add_argument("--service-token", default=None)
    ap.add_argument("--scenario-cameras", action="store_true",
                    help="use cameras from the YAML instead of backend /api/maps/current")
    args = ap.parse_args()
    sc = Scenario.load(args.scenario)

    from .._aio import run as aio_run

    try:
        if args.scenario_cameras:
            sc.use_current_map = False
        bound = aio_run(sc.bind_current_map(args.backend_url, args.service_token))
        source = "backend current map" if bound else "scenario yaml"
        if not sc.cameras:
            raise SystemExit("fake_pi: no cameras available; save/activate a map or provide scenario cameras")
        print(
            f"fake_pi: scenario={args.scenario} store={sc.store_id} "
            f"cameras={len(sc.cameras)} source={source}"
        )
        aio_run(FakePi(sc, args.host, args.port).run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
