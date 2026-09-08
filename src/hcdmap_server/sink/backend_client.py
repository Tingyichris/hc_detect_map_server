"""Idempotent one-way result feed to the backend, with a durable local retry queue.

Every result gets a deterministic event_id (uuid5 over stable fields) so retries and a
fusion restart produce the same id -> backend upserts, no duplicates. If the backend is
unreachable, items are appended to a jsonl queue on disk and drained in the background.
Never blocks the caller.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from pathlib import Path
from typing import Any

import httpx

from ..config import Config

log = logging.getLogger(__name__)

_NS = uuid.UUID("6f5a1d2e-0000-4000-8000-000000000000")

_ROUTES = {
    "pickup": "/api/ingest/pickups",
    "anomaly": "/api/ingest/anomalies",
    "camera_status": "/api/ingest/camera-status",
    "crossings": "/api/ingest/checkpoint-crossings",
}


def event_id(kind: str, *parts: Any) -> str:
    return str(uuid.uuid5(_NS, kind + "|" + "|".join(str(p) for p in parts)))


class BackendSink:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._queue_path = Path(cfg.backend.retry_queue_path)
        self._queue_path.parent.mkdir(parents=True, exist_ok=True)
        self._pending: asyncio.Queue[tuple[str, dict]] = asyncio.Queue()
        self._client = httpx.AsyncClient(
            base_url=cfg.backend.url,
            headers={"Authorization": f"Bearer {cfg.backend.service_token}"},
            timeout=10,
        )

    def submit(self, kind: str, body: dict) -> None:
        """Fire-and-forget. `body` must already contain event_id + store_id."""
        body.setdefault("store_id", self.cfg.store_id)
        self._pending.put_nowait((kind, body))

    async def run(self) -> None:
        await self._drain_disk_queue()
        while True:
            kind, body = await self._pending.get()
            ok = await self._post(kind, body)
            if not ok:
                self._append_disk(kind, body)
            self._pending.task_done()

    async def _post(self, kind: str, body: dict) -> bool:
        route = _ROUTES.get(kind)
        if route is None:
            log.error("unknown result kind %s", kind)
            return True  # drop; nothing we can do
        try:
            r = await self._client.post(route, json=body)
            if r.status_code >= 500:
                return False
            r.raise_for_status()
            return True
        except (httpx.HTTPError, httpx.TransportError) as e:
            log.warning("backend post %s failed: %s", kind, e)
            return False

    def _append_disk(self, kind: str, body: dict) -> None:
        with self._queue_path.open("a") as f:
            f.write(json.dumps({"kind": kind, "body": body}) + "\n")

    async def _drain_disk_queue(self) -> None:
        if not self._queue_path.exists():
            return
        lines = self._queue_path.read_text().splitlines()
        remaining: list[str] = []
        for line in lines:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not await self._post(item["kind"], item["body"]):
                remaining.append(line)
        self._queue_path.write_text("\n".join(remaining) + ("\n" if remaining else ""))
        log.info("disk retry queue drained; %d still pending", len(remaining))
