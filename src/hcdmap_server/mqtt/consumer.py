"""Subscribe to the store's MQTT tree, validate, drop-detect, quarantine, normalise.

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
        self._last_seq: dict[str, int] = {}

    def _topic_root(self) -> str:
        return f"sushi/{self.cfg.store_id}"

    def _check_seq(self, cam: str, seq: int) -> int:
        """Return number of dropped messages since last seen for this camera (0 if fine)."""
        prev = self._last_seq.get(cam)
        self._last_seq[cam] = seq
        if prev is None or seq <= prev:
            return 0
        return seq - prev - 1

    def _parse(self, topic: str, payload: bytes) -> NormalIn:
        root = self._topic_root()
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            return Quarantined(topic, "invalid_json", payload, time.time())

        try:
            if topic.startswith(f"{root}/events/"):
                ev = PlateEvent.model_validate(obj)
                skew = abs(ev.ts - time.time())
                if skew > self.cfg.fusion.max_clock_skew_s:
                    return Quarantined(topic, f"clock_skew={skew:.1f}s", payload, time.time())
                dropped = self._check_seq(ev.camera_id, ev.seq)
                if dropped:
                    log.warning("camera %s dropped %d messages", ev.camera_id, dropped)
                return ev
            if topic.startswith(f"{root}/status/"):
                return CameraStatus.model_validate(obj)
            if topic == f"{root}/belt/speed":
                return BeltSpeed.model_validate(obj)
            if topic == f"{root}/config/map":
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
                    clean_session=False,
                ) as client:
                    await client.subscribe(f"{self._topic_root()}/#", qos=1)
                    log.info("subscribed to %s/#", self._topic_root())
                    async for msg in client.messages:
                        yield self._parse(str(msg.topic), msg.payload)  # type: ignore[arg-type]
            except aiomqtt.MqttError as e:
                log.warning("mqtt disconnected (%s); retrying in 3s", e)
                await asyncio.sleep(3)
