---
name: replay
description: Replay a recorded MQTT capture (jsonl of {topic, payload, ts}) through the fusion engine for regression testing and debugging de-dup / pickup attribution. Use when the user has a capture file and wants to reproduce or diff fusion behaviour.
---

# Replay an MQTT capture through fusion

## Capture format

One JSON object per line: `{"topic": "sushi/store-001/events/cam_t02", "payload": {...}, "ts": 1725800000.1}`.
Record live traffic with:
```
mosquitto_sub -t 'sushi/#' -F '{"topic":"%t","payload":%p,"ts":%U}' > capture.jsonl
```

## Replay

There is no dedicated replay CLI yet — build one under `src/hcdmap_server/sim/replay.py` that:

1. Loads the map (from a local JSON fixture or a running backend).
2. Constructs a `FusionEngine` directly (no MQTT, no broker).
3. Iterates the capture in `ts` order, calling `engine.on_event(PlateEvent.model_validate(payload))`
   for `events/*` topics and feeding `camera_status` for `status/*`.
4. Calls `engine.tick(ts)` at a fixed cadence between messages.
5. Collects emitted pickups and prints a table: `gid, table_id, method, confidence, ts`.

Support `--speed N` (wall-clock multiplier) and `--assert expected_pickups.json` (diff emitted
vs expected for CI). Keep it dependency-free beyond the package itself.
