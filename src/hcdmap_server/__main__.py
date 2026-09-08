"""Wire the fusion server together: MQTT consume -> engine -> (sink + WS). Skeleton main loop."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from .config import load
from .fusion.engine import FusionEngine
from .fusion.pickup import detect as detect_pickup
from .map.model import TrackMap, fetch_current
from .mqtt.consumer import Consumer, Quarantined
from .mqtt.schema import CameraStatus, MapSignal, PlateEvent
from .sink.backend_client import BackendSink, event_id
from .ws.server import Hub, build_app, serve

log = logging.getLogger(__name__)
TICK_S = 0.5


async def _consume_loop(cfg, consumer: Consumer, engine_ref: dict, sink: BackendSink, hub: Hub):
    camera_states: dict[str, str] = {}
    async for msg in consumer.stream():
        engine: FusionEngine = engine_ref["engine"]
        if isinstance(msg, PlateEvent):
            engine.on_event(msg)
        elif isinstance(msg, CameraStatus):
            camera_states[msg.camera_id] = msg.state
            engine_ref["camera_states"] = camera_states
            await hub.broadcast({"type": "camera_status", "camera_id": msg.camera_id,
                                 "state": msg.state, "reason": msg.reason, "ts": msg.ts})
            sink.submit("camera_status", {
                "event_id": event_id("camstatus", msg.camera_id, msg.ts),
                "camera_id": msg.camera_id, "state": msg.state, "reason": msg.reason, "ts": msg.ts})
        elif isinstance(msg, MapSignal):
            log.info("map signal v%d -> reloading", msg.map_version)
            new_map = await fetch_current(cfg.backend.url, cfg.backend.service_token)
            engine_ref["engine"] = FusionEngine(cfg, new_map)   # TODO: carry over live plates
        elif isinstance(msg, Quarantined):
            sink.submit("anomaly", {
                "event_id": event_id("quarantine", msg.topic, msg.ts),
                "kind": "bad_payload", "detail": f"{msg.topic}: {msg.reason}",
                "ts": msg.ts, "severity": "warn"})


async def _tick_loop(cfg, engine_ref: dict, sink: BackendSink, hub: Hub):
    while True:
        now = time.time()
        engine: FusionEngine = engine_ref["engine"]
        camera_states = engine_ref.get("camera_states", {})
        engine.tick(now)

        # Emit live plate snapshot to the frontend.
        plates = [
            {"gid": p.gid, "s": round(p.s, 5), "class": p.plate_class, "status": p.status,
             "seen_by": list(p.seen_by), "confidence": round(p.confidence, 3)}
            for p in engine.plates.values() if p.status != "lost"
        ]
        by_class: dict[str, int] = {}
        for p in plates:
            by_class[p["class"]] = by_class.get(p["class"], 0) + 1
        await hub.broadcast({"type": "plates", "ts": now, "plates": plates,
                             "count_total": len(plates), "count_by_class": by_class,
                             "integrity": "estimated"})
        await hub.broadcast({"type": "belt", "moving": True,
                             "loop_seconds_est": round(engine.travel.loop_seconds_estimate(), 1),
                             "ts": now})

        # TODO: pull overdue crossings out of engine.tick and run detect_pickup() per segment,
        # then sink.submit("pickup", {...}) with a deterministic event_id and broadcast it.
        _ = detect_pickup
        await asyncio.sleep(TICK_S)


async def main_async() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load()
    log.info("store=%s broker=%s:%s backend=%s", cfg.store_id, cfg.mqtt.host, cfg.mqtt.port,
             cfg.backend.url)

    track_map: TrackMap = await fetch_current(cfg.backend.url, cfg.backend.service_token)
    engine_ref = {"engine": FusionEngine(cfg, track_map), "camera_states": {}}
    sink = BackendSink(cfg)
    hub = Hub()
    ws_app = build_app(hub)
    consumer = Consumer(cfg)

    await asyncio.gather(
        sink.run(),
        serve(ws_app, cfg.ws.host, cfg.ws.port),
        _consume_loop(cfg, consumer, engine_ref, sink, hub),
        _tick_loop(cfg, engine_ref, sink, hub),
    )


def main() -> None:
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main_async())


if __name__ == "__main__":
    main()
