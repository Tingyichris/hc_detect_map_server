"""Subscribe to the store's MQTT tree, validate, quarantine, normalise.

Emits internal events onto an asyncio.Queue consumed by fusion.engine. Never does backend I/O.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass

import aiomqtt
from pydantic import ValidationError

from ..config import Config
from .schema import BeltSpeed, CameraStatus, MapSignal, PlateEvent

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Quarantined:
    topic: str
    reason: str
    raw: bytes
    ts: float


NormalIn = PlateEvent | CameraStatus | BeltSpeed | MapSignal | Quarantined


class Consumer:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg

    def _parse(self, topic: str, payload: bytes) -> NormalIn:
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            return Quarantined(topic, "invalid_json", payload, time.time())

        try:
            if topic.startswith("sushi/events/"):
                obj["camera_id"] = topic.removeprefix("sushi/events/")
                obj["ts"] = time.time()
                ev = PlateEvent.model_validate(obj)
                return ev
            if topic.startswith("sushi/status/"):
                obj["camera_id"] = topic.removeprefix("sushi/status/")
                obj["ts"] = time.time()
                obj.setdefault("reason", "ok")
                return CameraStatus.model_validate(obj)
            root = f"sushi/{self.cfg.store_id}"
            if topic in {"sushi/belt/speed", f"{root}/belt/speed"}:
                return BeltSpeed.model_validate(obj)
            if topic in {"sushi/config/map", f"{root}/config/map"}:
                return MapSignal.model_validate(obj)
        except ValidationError as e:
            return Quarantined(topic, f"schema:{e.error_count()}errs", payload, time.time())
        return Quarantined(topic, "unknown_topic", payload, time.time())

    async def stream(self) -> AsyncIterator[NormalIn]:
        """Yield normalised messages forever, reconnecting on broker loss."""
        while True:
            try:
                async with aiomqtt.Client(
                    hostname=self.cfg.mqtt.host,
                    port=self.cfg.mqtt.port,
                    identifier=self.cfg.mqtt.client_id,
                    username=self.cfg.mqtt.username or None,
                    password=self.cfg.mqtt.password or None,
                    protocol=aiomqtt.ProtocolVersion.V311,
                    clean_session=False,
                ) as client:
                    await client.subscribe("sushi/#", qos=1)
                    log.info("subscribed to sushi/#")
                    async for msg in client.messages:
                        yield self._parse(str(msg.topic), msg.payload)  # type: ignore[arg-type]
            except aiomqtt.MqttError as e:
                log.warning("mqtt disconnected (%s); retrying in 3s", e)
                await asyncio.sleep(3)
