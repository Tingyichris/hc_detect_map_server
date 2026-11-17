"""Wire the fusion server together: MQTT consume -> engine -> (sink + WS). Skeleton main loop."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from .config import load
from .fusion.engine import FusionEngine
from .fusion.pickup import Pickup, detect as detect_pickup
from .map.model import TrackMap, fetch_current
from .mqtt.consumer import Consumer, Quarantined
from .mqtt.schema import CameraStatus, MapSignal, PlateEvent
from .sink.backend_client import BackendSink, event_id
from .ws.server import Hub, build_app, serve

log = logging.getLogger(__name__)
# Live positions are lightweight and need to reach the dashboard while a plate is
# still near the camera. 500 ms made the UI visibly trail fast conveyor motion.
TICK_S = 0.1


async def _consume_loop(cfg, consumer: Consumer, engine_ref: dict, sink: BackendSink, hub: Hub):
    camera_states: dict[str, str] = {}
    async for msg in consumer.stream():
        engine: FusionEngine = engine_ref["engine"]
        if isinstance(msg, PlateEvent):
            logical_id = engine.map.source_to_camera.get(msg.source_id or msg.camera_id, msg.camera_id)
            engine.on_event(msg.model_copy(update={"camera_id": logical_id}))
        elif isinstance(msg, CameraStatus):
            source_id = msg.source_id or msg.camera_id
            logical_id = engine.map.source_to_camera.get(source_id, msg.camera_id)
            camera_states[logical_id] = msg.state
            engine_ref["camera_states"] = camera_states
            await hub.broadcast({"type": "camera_status", "camera_id": logical_id,
                                 "source_id": source_id,
                                 "state": msg.state, "reason": msg.reason, "ts": msg.ts})
            sink.submit("camera_status", {
                "event_id": event_id("camstatus", source_id, msg.ts),
                "camera_id": source_id, "state": msg.state, "reason": msg.reason, "ts": msg.ts})
        elif isinstance(msg, MapSignal):
            log.info("map signal v%d -> reloading", msg.map_version)
            new_map = await fetch_current(cfg.backend.url, cfg.backend.service_token)
            engine.update_map(new_map)
        elif isinstance(msg, Quarantined):
            sink.submit("anomaly", {
                "event_id": event_id("quarantine", msg.topic, msg.ts),
                "kind": "bad_payload", "detail": f"{msg.topic}: {msg.reason}",
                "ts": msg.ts, "severity": "warn"})


def _pickup_body(cfg, pickup: Pickup) -> dict:
    return {
        "event_id": event_id(
            "pickup",
            pickup.gid,
            pickup.segment[0],
            pickup.segment[1],
            pickup.ts,
            pickup.method,
        ),
        "store_id": cfg.store_id,
        "gid": pickup.gid,
        "table_id": pickup.table_id,
        "camera_id": pickup.camera_id,
        "class": pickup.plate_class,
        "ts": pickup.ts,
        "confidence": pickup.confidence,
        "method": pickup.method,
        "segment": pickup.segment,
        "kitchen_removal": pickup.kitchen_removal,
        "integrity": pickup.integrity,
    }


def _occlusion_anomaly_body(cfg, pickup: Pickup) -> dict:
    return {
        "event_id": event_id("pickup-occlusion", pickup.gid, pickup.camera_id, pickup.ts),
        "store_id": cfg.store_id,
        "kind": "camera_occluded",
        "camera_id": pickup.camera_id,
        "detail": (
            f"pickup suspected for {pickup.gid} in segment "
            f"{pickup.segment[0]}>{pickup.segment[1]} while camera was degraded/offline"
        ),
        "ts": pickup.ts,
        "severity": "warn",
    }


async def _tick_loop(cfg, engine_ref: dict, sink: BackendSink, hub: Hub):
    while True:
        now = time.time()
        engine: FusionEngine = engine_ref["engine"]
        camera_states = engine_ref.get("camera_states", {})
        overdue = engine.tick(now)

        for uc in overdue:
            pickup = detect_pickup(
                uc,
                engine.map,
                camera_states=camera_states,
                offbelt_gids=engine.offbelt_gids,
                coldstart_done=False,
            )
            if pickup is None:
                continue
            # A catch or an overdue unmatched crossing is now a confirmed removal. Keep the
            # backend/table event and the live map inventory in the same state.
            engine.mark_pickup_emitted(pickup.gid)
            body = _pickup_body(cfg, pickup)
            sink.submit("pickup", body)
            await hub.broadcast(
                {
                    "type": "pickup",
                    "table_id": pickup.table_id,
                    "class": pickup.plate_class,
                    "gid": pickup.gid,
                    "ts": pickup.ts,
                    "confidence": pickup.confidence,
                }
            )
            if pickup.method == "occluded_suspect":
                sink.submit("anomaly", _occlusion_anomaly_body(cfg, pickup))

        # Emit live plate snapshot to the frontend.
        plates = [
            {"gid": p.gid, "s": round(p.s, 5), "class": p.plate_class, "color": p.color,
             "status": p.status,
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

    async def reset_live_state() -> None:
        engine_ref["engine"].reset_live_state()
        await hub.broadcast({
            "type": "plates", "ts": time.time(), "plates": [],
            "count_total": 0, "count_by_class": {}, "integrity": "estimated",
        })

    ws_app = build_app(hub, reset_live_state)
    consumer = Consumer(cfg)

    await asyncio.gather(
        sink.run(),
        serve(ws_app, cfg.ws.host, cfg.ws.port),
        _consume_loop(cfg, consumer, engine_ref, sink, hub),
        _tick_loop(cfg, engine_ref, sink, hub),
    )


def main() -> None:
    from ._aio import run as aio_run

    with contextlib.suppress(KeyboardInterrupt):
        aio_run(main_async())


if __name__ == "__main__":
    main()
