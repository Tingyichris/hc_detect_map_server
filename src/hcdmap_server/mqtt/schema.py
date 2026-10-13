"""Pydantic models for the Pi -> fusion MQTT payloads. Mirrors docs/mqtt-contract.md (v1)."""
from __future__ import annotations

import time
from typing import Literal

from pydantic import BaseModel, Field

CONTRACT_VERSION = 1

EventKind = Literal["entry", "exit", "catch"]
CamState = Literal["online", "degraded", "offline"]
CamReason = Literal["ok", "occluded", "low_light", "moved", "rtsp_lost", "lwt"]


class PlateEvent(BaseModel):
    """Payload of sushi/events/<camera_id>."""

    camera_id: str  # populated from the topic by Consumer
    ts: float = Field(default_factory=time.time)  # receive time; not in payload
    event: EventKind
    plate_id: int = Field(ge=0)
    plate_class: str = Field(alias="class")
    color: str = "#8892a6"

    model_config = {"populate_by_name": True, "extra": "ignore"}


class CameraStatus(BaseModel):
    """Payload of sushi/status/<camera_id> (retained)."""

    camera_id: str  # populated from the topic by Consumer
    state: CamState
    ts: float = Field(default_factory=time.time)
    reason: CamReason = "ok"

    model_config = {"extra": "ignore"}


class BeltSpeed(BaseModel):
    """Payload of sushi/<store>/belt/speed (optional, advisory only)."""

    v: int = Field(default=CONTRACT_VERSION)
    mps: float = Field(ge=0.0)
    ts: float

    model_config = {"extra": "ignore"}


class MapSignal(BaseModel):
    """Payload of sushi/<store>/config/map (published by backend)."""

    v: int = Field(default=CONTRACT_VERSION)
    map_version: int
    ts: float

    model_config = {"extra": "ignore"}
