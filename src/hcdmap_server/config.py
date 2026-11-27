"""Load config from TOML with HCDMAP_* env-var overrides."""
from __future__ import annotations

import os
from dataclasses import dataclass, field, fields

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover - 3.10 fallback
    import tomli as tomllib
from pathlib import Path
from typing import Any

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "config" / "server.toml"


@dataclass(slots=True)
class MqttCfg:
    host: str = "localhost"
    port: int = 1883
    username: str = ""
    password: str = ""
    client_id: str = "hcdmap-server"


@dataclass(slots=True)
class BackendCfg:
    url: str = "http://localhost:8000"
    service_token: str = "dev-service-token"
    retry_queue_path: str = "state/retry_queue.jsonl"
    max_retry_queue: int = 50000


@dataclass(slots=True)
class WsCfg:
    host: str = "0.0.0.0"
    port: int = 8001


@dataclass(slots=True)
class FusionCfg:
    match_window_s: float = 2.0
    match_radius_s: float = 0.01
    reacquire_window_s: float = 1.5
    # Deprecated compatibility setting. Retained MQTT session state is authoritative.
    camera_offline_after_s: float = 30.0
    max_clock_skew_s: float = 3.0


@dataclass(slots=True)
class StateCfg:
    snapshot_path: str = "state/track.snapshot"
    snapshot_interval_s: float = 10.0
    coldstart_replay_s: float = 180.0


@dataclass(slots=True)
class Config:
    store_id: str = "store-001"
    mqtt: MqttCfg = field(default_factory=MqttCfg)
    backend: BackendCfg = field(default_factory=BackendCfg)
    ws: WsCfg = field(default_factory=WsCfg)
    fusion: FusionCfg = field(default_factory=FusionCfg)
    state: StateCfg = field(default_factory=StateCfg)


def _apply_env(prefix: str, obj: Any) -> None:
    for f in fields(obj):
        env_key = f"{prefix}_{f.name}".upper()
        if env_key in os.environ:
            raw = os.environ[env_key]
            cur = getattr(obj, f.name)
            setattr(obj, f.name, type(cur)(raw) if not isinstance(cur, bool) else raw == "1")


def load(path: Path | None = None) -> Config:
    path = path or DEFAULT_PATH
    data: dict[str, Any] = {}
    if path.exists():
        data = tomllib.loads(path.read_text())

    cfg = Config(
        store_id=data.get("store_id", "store-001"),
        mqtt=MqttCfg(**data.get("mqtt", {})),
        backend=BackendCfg(**data.get("backend", {})),
        ws=WsCfg(**data.get("ws", {})),
        fusion=FusionCfg(**data.get("fusion", {})),
        state=StateCfg(**data.get("state", {})),
    )

    if "HCDMAP_STORE_ID" in os.environ:
        cfg.store_id = os.environ["HCDMAP_STORE_ID"]
    _apply_env("HCDMAP_MQTT", cfg.mqtt)
    _apply_env("HCDMAP_BACKEND", cfg.backend)
    _apply_env("HCDMAP_WS", cfg.ws)
    _apply_env("HCDMAP_FUSION", cfg.fusion)
    _apply_env("HCDMAP_STATE", cfg.state)
    return cfg
