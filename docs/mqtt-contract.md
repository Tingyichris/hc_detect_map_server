# MQTT contract — Pi → fusion server

This is the integration boundary between the per-camera Raspberry Pis and `server/`.
The Pi side must conform to this compact event contract.

## Broker

- Mosquitto (dev: anonymous; prod: per-Pi username/password + ACL).
- Each Pi: fixed `client_id = <camera_id>`, `clean_session = false`, so a short fusion
  restart doesn't lose events (broker holds QoS 1 messages for the offline session).

## Topics

| Topic | QoS | Retain | Publisher |
|-------|-----|--------|-----------|
| `sushi/events/<camera_id>` | 1 | no  | Pi — plate lifecycle events |
| `sushi/status/<camera_id>` | 1 | yes | Pi — camera liveness (+ LWT) |
| `sushi/belt/speed`         | 0 | yes | optional — PLC/encoder feed |
| `sushi/config/map`         | 1 | yes | backend — map-reload signal |

## `events/<camera_id>` payload

```json
{
  "event": "entry",
  "plate_id": 17,
  "class": "red",
  "color": "#e0564c"
}
```

| field | meaning |
|-------|---------|
| `event` | `entry` — Plate ID 通過 ENTRY · `exit` — 從出口離開 · `catch` — 在出口前被取走 |
| `plate_id` | 相機內的持久 Plate ID，用來關聯同一相機的 entry 與最終事件。 |
| `class` | plate type string; must be one of the classes in `plate_classes` config. |
| `color` | UI colour in `#RRGGBB` format from the camera's stable colour result. |

Optional extra fields are allowed and ignored by fusion.

## `status/<camera_id>` payload (retained)

```json
{ "state": "online" }
```

- `state`: `online` · `degraded` · `offline`
- Published on connect (`online`), on any state change, and as a heartbeat every ~10 s.
- **LWT** registered on the same topic as `{"state":"offline"}`, retained.
- Fusion: no message for > 30 s ⇒ treat as `offline` regardless.

## `belt/speed` payload (optional, retained)

```json
{ "v": 1, "mps": 0.12, "ts": 1725800000.0 }
```

Advisory only. Fusion primarily learns per-segment travel time from matched plates; this
just helps bridge sudden speed changes before the estimator catches up.

## `config/map` payload (retained, published by backend)

```json
{ "v": 1, "map_version": 7, "ts": 1725800000.0 }
```

Fusion on receipt: fetch `GET <backend>/api/maps/current`, recompute camera intervals and
segments, hot-swap without dropping current track state.

## Pi-side occlusion detection

The Pi should self-report `state:"degraded", reason:"occluded"` when: a large fraction of the
frame is low-texture / uniform / suddenly dark and persists, or the belt-line ROI / reference
features vanish. This lets fusion switch that segment to conservation-only mode and raise a
staff alert.
