# hcdmap-server — fusion engine

Standalone project. Consumes per-camera MQTT (detection runs on the Pis, not here), de-dups,
reconstructs the conveyor track state, detects plate pickups and attributes them to tables.
Pushes results to `hcdmap-backend` over HTTP; streams live plate positions to `hcdmap-frontend`
over WebSocket (:8001).

```
pip install -e ".[dev]"
docker compose up -d
python -m hcdmap_server
python -m hcdmap_server.sim.fake_pi --scenario normal
pytest
```

See `CLAUDE.md` for the module map and design decisions, `docs/` for the wire contracts.
