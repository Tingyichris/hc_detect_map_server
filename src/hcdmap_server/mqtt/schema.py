"""Pydantic models for the Pi -> fusion MQTT payloads. Mirrors docs/mqtt-contract.md (v1)."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

CONTRACT_VERSION = 1

EventKind = Literal["enter", "update", "exit"]
ExitSide = Literal["downstream", "upstream", "offbelt", "unknown"]
CamState = Literal["online", "degraded", "offline"]
CamReason = Literal["ok", "occluded", "low_light", "moved", "rtsp_lost", "lwt"]


class PlateEvent(BaseModel):
    """Payload of sushi/<store>/events/<camera_id>."""

    v: int = Field(default=CONTRACT_VERSION)
    camera_id: str
    seq: int = Field(ge=0)
    ts: float
    event: EventKind
    track_id: int = Field(ge=0)
    plate_class: str = Field(alias="class")
    conf: float = Field(ge=0.0, le=1.0)
    pos_norm: float | None = Field(default=None, ge=0.0, le=1.0)
    exit_side: ExitSide | None = None

    model_config = {"populate_by_name": True, "extra": "ignore"}


class CameraStatus(BaseModel):
    """Payload of sushi/<store>/status/<camera_id> (retained)."""

    v: int = Field(default=CONTRACT_VERSION)
    camera_id: str
    state: CamState
    ts: float
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
