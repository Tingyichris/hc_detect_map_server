# MQTT contract — Pi → fusion server

This is the integration boundary between the per-camera Raspberry Pis and `server/`.
The Pi side must conform to this. Version it; breaking changes bump `v`.

Contract version: **1**

## Broker

- Mosquitto (dev: anonymous; prod: per-Pi username/password + ACL).
- Each Pi: fixed `client_id = <camera_id>`, `clean_session = false`, so a short fusion
  restart doesn't lose events (broker holds QoS 1 messages for the offline session).
- All Pis run NTP against a common server. Matching tolerates ~±0.5 s clock skew.

## Topics (namespaced by store)

| Topic | QoS | Retain | Publisher |
|-------|-----|--------|-----------|
| `sushi/<store_id>/events/<camera_id>` | 1 | no  | Pi — plate lifecycle events |
| `sushi/<store_id>/status/<camera_id>` | 1 | yes | Pi — camera liveness (+ LWT) |
| `sushi/<store_id>/belt/speed`         | 0 | yes | optional — PLC/encoder feed |
| `sushi/<store_id>/config/map`          | 1 | yes | backend — map-reload signal |

## `events/<camera_id>` payload

```json
{
  "v": 1,
  "camera_id": "cam_t01",
  "seq": 10432,
  "ts": 1725800000.123,
  "event": "enter",
  "track_id": 17,
  "class": "red",
  "conf": 0.91,
  "pos_norm": 0.42,
  "exit_side": null
}
```

| field | meaning |
|-------|---------|
| `v` | contract version (int) |
| `seq` | monotonic per camera. Fusion uses gaps to detect dropped messages. |
| `ts` | epoch seconds, Pi clock (NTP-synced). |
| `event` | `enter` — new `track_id` appeared · `update` — periodic position while visible (every ~0.5–1 s) · `exit` — `track_id` left the view |
| `track_id` | stable **only within this camera while the plate is visible**. May be reused later. Meaningless across cameras. |
| `class` | plate type string; must be one of the classes in `plate_classes` config. |
| `conf` | detector confidence 0–1. |
| `pos_norm` | position within this camera's view span: `0.0` = upstream edge, `1.0` = downstream edge. `null` if the Pi can't compute it (fusion falls back to bbox-x within a configured ROI). |
| `exit_side` | only on `event="exit"`: `downstream` (left via downstream edge — continued on belt) · `upstream` (rare — belt reversed) · `offbelt` (disappeared mid-view / bbox moved up out of the belt band → **picked up**) · `unknown` (Pi couldn't tell → fusion infers from downstream non-arrival) |

Optional extra fields are allowed and ignored by fusion.

## `status/<camera_id>` payload (retained)

```json
{ "v": 1, "camera_id": "cam_t01", "state": "online", "ts": 1725800000.0, "reason": "ok" }
```

- `state`: `online` · `degraded` · `offline`
- `reason`: `ok` · `occluded` · `low_light` · `moved` · `rtsp_lost` · `lwt`
- Published on connect (`online`), on any state change, and as a heartbeat every ~10 s.
- **LWT** registered on the same topic as `{"state":"offline","reason":"lwt"}`, retained.
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
