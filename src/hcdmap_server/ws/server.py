"""WebSocket fan-out to the frontend: live plate positions, camera status, pickups, belt.

Ephemeral only — nothing here is persisted (that path is server -> backend HTTP). Runs as a
FastAPI app on :8001. Token check is a stub; wire it to backend GET /api/ws-token.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect


class Hub:
    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._clients.add(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self._clients.discard(ws)

    async def broadcast(self, message: dict[str, Any]) -> None:
        dead = []
        payload = json.dumps(message)
        for ws in self._clients:
            try:
                await ws.send_text(payload)
            except (WebSocketDisconnect, RuntimeError):
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


def build_app(
    hub: Hub,
    reset_live_state: Callable[[], Awaitable[None]] | None = None,
) -> FastAPI:
    app = FastAPI(title="hcdmap-server live feed")

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        # TODO: verify ws.query_params["token"] against backend.
        await hub.connect(ws)
        try:
            while True:
                raw = await ws.receive_text()
                try:
                    command = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if (
                    reset_live_state is not None
                    and command.get("action") == "reset_live_state"
                ):
                    await reset_live_state()
        except WebSocketDisconnect:
            hub.disconnect(ws)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


async def serve(app: FastAPI, host: str, port: int) -> None:
    import uvicorn

    config = uvicorn.Config(app, host=host, port=port, log_level="info")
    server = uvicorn.Server(config)
    with contextlib.suppress(asyncio.CancelledError):
        await server.serve()
