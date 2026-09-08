# CLAUDE.md — hcdmap server (fusion engine)

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Standalone project. Sibling projects `backend/` and `frontend/` are separate repos; this one
talks to them only over HTTP (results feed) and WebSocket (live feed). Contracts it must honour
are in `docs/`: `mqtt-contract.md`, `api-contract.md`, `map-schema.md`.

## What it does

Consumes the per-camera MQTT streams from the Raspberry Pis (detection runs on the Pis — NOT
here), and:
- de-duplicates plates seen by multiple cameras,
- reconstructs which plates are on the conveyor track right now,
- detects plates that leave the belt and attributes each to the table where it disappeared.

Results (pickups, anomalies, camera status) are POSTed to `backend`. Live plate positions are
pushed to `frontend` over WebSocket on :8001. Fusion holds its own restart state locally.

## Run

```
pip install -e ".[dev]"
docker compose up -d                      # local Mosquitto broker
python -m hcdmap_server                    # needs backend reachable for GET /api/maps/current
python -m hcdmap_server.sim.fake_pi --scenario normal   # traffic without hardware (see /sim)
pytest
```

Config: `config/server.toml` (copy `server.example.toml`), overridable by `HCDMAP_*` env vars.

## Module map

| Module | Responsibility |
|--------|----------------|
| `mqtt/schema.py` | pydantic models for the MQTT payloads (mirror `docs/schemas/`). |
| `mqtt/consumer.py` | subscribe `sushi/<store>/#`, validate, drop-detect via `seq`, quarantine bad payloads / large clock skew. |
| `map/model.py` | typed map (`Track`, `Camera`, `Segment`); load from `GET backend/api/maps/current`; hot-reload on `config/map`. |
| `map/geometry.py` | polyline → arc-length `s`; project pixel → `s`; loop distance with wrap; segments from counting lines. **Pure, unit-tested.** |
| `fusion/travel_time.py` | online per-segment travel-time estimator (median of recent matched Δt). Replaces belt speed; seeded from `approx_loop_seconds`. |
| `fusion/checkpoints.py` | per counting-line crossing log + FIFO sequence alignment between adjacent lines. |
| `fusion/engine.py` | global plate list (`gid`), overlap binding, re-acquire hysteresis, position fusion. |
| `fusion/pickup.py` | overdue crossing → disappearance → table attribution (`offbelt_confirmed` / `conservation` / `occluded_suspect`). |
| `fusion/coldstart.py` | inventory plates already on the belt over one loop; counts `estimated` until done. |
| `track/state.py` | `TrackState`: plates, per-table tallies, integrity. Snapshot-serialisable. |
| `state/snapshot.py` | periodic local snapshot + restore (survives backend downtime). |
| `sink/backend_client.py` | idempotent `POST` to backend with deterministic `event_id`; durable local retry queue. |
| `ws/server.py` | FastAPI WS on :8001 → `plates` / `camera_status` / `pickup` / `belt`. |

## Key design decisions

- **1D track coordinate `s`** is derived from the map's clicked polyline; never hand-authored.
- **No belt-speed input.** Per-segment travel time is learned online from matched plates.
- **Pickup detection is upstream/downstream conservation, not the table's own camera** — it
  survives a camera being covered. The table's camera only refines attribution and provides
  `exit_side=offbelt` confirmation. See `docs/` and `fusion/pickup.py` docstring.
- **`server → backend` is a one-way idempotent feed.** `event_id` = uuid5 of stable fields;
  backend upserts. Local retry queue; pickups are never dropped.
- **Camera roles:** `table` (has `table_id`, billable) or `source` (kitchen output — injection
  count for whole-loop conservation; a "pickup" there is a non-billable `kitchen_removal`).

## Invariants

- `track_id` is per-camera; only `gid` crosses cameras.
- A `gid` crosses each counting line at most once.
- Never block the MQTT consume loop on backend I/O — `sink` is fire-and-queue.
- All timestamps are epoch seconds; base matching tolerance ~1.5–2 s; hosts run NTP.

## Conventions

- ruff for lint + format (hook on save). `src/` layout. Python ≥ 3.11, asyncio throughout.
