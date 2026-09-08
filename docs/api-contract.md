# API contract

Two HTTP surfaces + two WebSocket surfaces.

## 1. `server → backend` result feed (HTTP, idempotent, one-way)

The fusion server pushes durable results here. Every item carries a stable `event_id`
(UUIDv4 derived deterministically from `(store_id, kind, source_seq...)` so retries collide).
Backend upserts on `event_id` (unique constraint) — safe to POST the same item many times.
Server keeps a local retry queue; backend downtime never drops results.

Auth: `Authorization: Bearer <service-token>` (shared secret, per deployment).

### `POST /api/ingest/pickups`
```json
{
  "event_id": "b1d0...-...",
  "store_id": "store-001",
  "gid": "p_000417",
  "table_id": "T02",
  "camera_id": "cam_t02",
  "class": "gold",
  "ts": 1725800001.05,
  "confidence": 0.74,
  "method": "conservation",            // conservation | offbelt_confirmed | occluded_suspect
  "segment": ["cam_t02", "cam_t03"],
  "kitchen_removal": false,
  "integrity": "ok"                    // ok | estimated | degraded
}
```

### `POST /api/ingest/checkpoint-crossings`  (optional audit stream, batched)
```json
{ "event_id": "...", "store_id": "store-001",
  "crossings": [
    {"camera_id": "cam_t01", "gid": "p_000417", "class": "red", "ts": 1725800000.98}
  ] }
```

### `POST /api/ingest/anomalies`
```json
{
  "event_id": "...", "store_id": "store-001",
  "kind": "camera_occluded",           // camera_occluded | conservation_mismatch | low_confidence_pickup | clock_skew | bad_payload | belt_stopped
  "camera_id": "cam_t02",
  "detail": "occluded 42s while 3 plates entered segment",
  "ts": 1725800040.0,
  "severity": "warn"                   // info | warn | critical
}
```

### `POST /api/ingest/camera-status`
```json
{ "event_id": "...", "store_id": "store-001",
  "camera_id": "cam_t02", "state": "degraded", "reason": "occluded", "ts": 1725800040.0 }
```

## 2. `frontend → backend` REST

Auth: user login (cookie/JWT), roles `floor` and `manager`.

| Method | Path | Notes |
|--------|------|-------|
| `GET`  | `/api/maps` | list versions |
| `GET`  | `/api/maps/current` | active map (also fetched by fusion) |
| `GET`  | `/api/maps/{version}` | one version |
| `POST` | `/api/maps` | save new version (from editor); publishes `sushi/<store>/config/map` |
| `POST` | `/api/maps/{version}/activate` | make a version active |
| `POST` | `/api/floorplans` | upload floor-plan image (multipart) → returns id/url |
| `GET`  | `/api/cameras` | registry + last-seen + reliability stats |
| `GET`  | `/api/plate-classes` / `PUT` `/api/plate-classes` | class → price, colour |
| `GET`  | `/api/sessions?status=open` | table sessions |
| `POST` | `/api/sessions/{table_id}/open` · `/close` | manual lifecycle (until POS wired) |
| `GET`  | `/api/sessions/{id}` | full session incl. pickup list + bill |
| `POST` | `/api/sessions/{id}/reconcile` | attach POS/manual count, record discrepancy |
| `GET`  | `/api/pickups?session_id=` / `?table_id=` / `?since=` | |
| `GET`  | `/api/anomalies?status=open` | |
| `GET`  | `/api/review` | review queue (low-confidence / unattributed pickups) |
| `POST` | `/api/review/{id}/assign` | `{table_id}` |
| `POST` | `/api/review/{id}/dismiss` | |
| `POST` | `/api/corrections` | staff add/remove a plate on a table; `{table_id, delta, class, reason, user}` — always logged |
| `GET`  | `/api/health` | pipeline health: broker, server heartbeat, per-Pi last-seen, processing lag |

## 3. `server → frontend` WebSocket  (`ws://<server>:8001/ws`)

Ephemeral, high-frequency, never persisted. Token issued by backend (`GET /api/ws-token`).

Server → client messages:
```json
{"type": "plates", "ts": 1725800001.05,
 "plates": [
   {"gid": "p_000417", "s": 0.412, "class": "red",
    "status": "tracked",            // tracked | predicted | lost
    "seen_by": ["cam_t01"], "confidence": 0.86}
 ],
 "count_total": 23, "count_by_class": {"red": 9, "gold": 4, "black": 2, "green": 8},
 "integrity": "ok"}

{"type": "camera_status", "camera_id": "cam_t03", "state": "degraded", "reason": "occluded", "ts": 1725800040.0}

{"type": "pickup", "table_id": "T02", "class": "gold", "gid": "p_000390",
 "ts": 1725800001.0, "confidence": 0.74}

{"type": "belt", "moving": true, "loop_seconds_est": 108.0, "ts": 1725800001.0}
```

`s` is normalised arc-length `[0, 1)` along the track polyline. The frontend converts `s`
to an `(x, y)` pixel on the floor-plan image using the same polyline from `GET /api/maps/current`.

## 4. `frontend → backend` WebSocket  (`/api/ws`)

Push for persisted-data changes the dashboard cares about: `session_updated`,
`anomaly_opened`, `review_item_added`, `health_changed`. Frontend still reads full state via REST.
