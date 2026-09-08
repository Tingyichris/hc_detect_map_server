"""Fake Pis: publish realistic sushi/<store>/events + status so the whole backend can be
built and tested with no hardware.

Model: plates are injected at the `source` camera and ride a virtual loop at a configurable
speed. Each camera emits enter/update/exit as a plate passes through its span. A scenario can
schedule pickups (plate removed at a table -> exit_side=offbelt at that camera, no downstream
events) and occlusions (a camera goes degraded and stops emitting for a while).

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
import yaml

SCENARIO_DIR = Path(__file__).parent / "scenarios"


@dataclass
class SimCamera:
    id: str
    role: str
    s_start: float
    s_end: float


@dataclass
class Scenario:
    store_id: str
    loop_seconds: float
    inject_every_s: tuple[float, float]
    classes: dict[str, float]                     # class -> weight
    cameras: list[SimCamera]
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
            cameras=[SimCamera(**c) for c in raw["cameras"]],
            pickups=raw.get("pickups", []),
            occlusions=raw.get("occlusions", []),
        )


class FakePi:
    def __init__(self, sc: Scenario, host: str, port: int) -> None:
        self.sc = sc
        self.host, self.port = host, port
        self._seq = {c.id: itertools.count(1) for c in sc.cameras}
        self._track_ids = itertools.count(1)
        self._t0 = time.time()

    def _topic(self, kind: str, cam: str) -> str:
        return f"sushi/{self.sc.store_id}/{kind}/{cam}"

    async def run(self) -> None:
        async with aiomqtt.Client(self.host, self.port) as client:
            for c in self.sc.cameras:
                await self._status(client, c.id, "online", "ok")
            await asyncio.gather(
                self._heartbeats(client),
                self._occlusion_driver(client),
                self._plate_driver(client),
            )

    async def _status(self, client, cam, state, reason) -> None:
        await client.publish(
            self._topic("status", cam),
            json.dumps({"v": 1, "camera_id": cam, "state": state, "ts": time.time(),
                        "reason": reason}),
            qos=1, retain=True,
        )

    async def _heartbeats(self, client) -> None:
        while True:
            await asyncio.sleep(10)
            for c in self.sc.cameras:
                if c.id not in self._occluded:
                    await self._status(client, c.id, "online", "ok")

    _occluded: set[str] = set()

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
        cams = sorted(self.sc.cameras, key=lambda c: c.s_start)
        speed = 1.0 / self.sc.loop_seconds  # s-units per second

        while True:
            await asyncio.sleep(random.uniform(*self.sc.inject_every_s))
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
            enter_delay = cam.s_start / speed - (time.time() - born)
            if enter_delay > 0:
                await asyncio.sleep(enter_delay)
            if cam.id in self._occluded:
                continue  # camera blind: emits nothing
            tid = next(self._track_ids)
            span = max(cam.s_end - cam.s_start, 1e-4)
            await self._event(client, cam.id, "enter", tid, plate_class, 0.0)
            steps = 4
            for k in range(1, steps):
                await asyncio.sleep((span / speed) / steps)
                await self._event(client, cam.id, "update", tid, plate_class, k / steps)
            await asyncio.sleep((span / speed) / steps)
            if pickup and pickup["table_id"] == getattr(cam, "table_id", cam.id.replace("cam_", "T").upper()):
                await self._event(client, cam.id, "exit", tid, plate_class, 0.6, "offbelt")
                return  # taken here: no further cameras
            await self._event(client, cam.id, "exit", tid, plate_class, 1.0, "downstream")

    async def _event(self, client, cam, kind, tid, plate_class, pos, exit_side=None) -> None:
        payload = {"v": 1, "camera_id": cam, "seq": next(self._seq[cam]), "ts": time.time(),
                   "event": kind, "track_id": tid, "class": plate_class,
                   "conf": round(random.uniform(0.8, 0.98), 3), "pos_norm": round(pos, 3),
                   "exit_side": exit_side}
        await client.publish(self._topic("events", cam), json.dumps(payload), qos=1)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", default="normal")
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=1883)
    args = ap.parse_args()
    sc = Scenario.load(args.scenario)
    print(f"fake_pi: scenario={args.scenario} store={sc.store_id} cameras={len(sc.cameras)}")
    try:
        asyncio.run(FakePi(sc, args.host, args.port).run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
