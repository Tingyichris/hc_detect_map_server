---
name: sim
description: Run the fake-Pi MQTT traffic generator against the local broker so the fusion server (and downstream backend/frontend) can be exercised with no hardware. Use when the user wants to test, demo, or debug the pipeline end to end.
---

# Run the fake Pi simulator

Publishes realistic `sushi/<store>/events` and `sushi/<store>/status` messages for a whole set
of cameras, driving virtual plates around a loop.

## Steps

1. Make sure a broker is up: `docker compose up -d` (starts Mosquitto on :1883).
2. Pick a scenario from `src/hcdmap_server/sim/scenarios/`:
   - `normal` — steady traffic, no pickups, overlap + gap coverage mix.
   - `pickup` — scheduled plate pickups at named tables (`exit_side=offbelt`).
   - `occlusion` — a camera goes `degraded` and blind while its table takes plates; detection
     must still fire via upstream/downstream conservation (`method=occluded_suspect`).
3. Run it:
   ```
   python -m hcdmap_server.sim.fake_pi --scenario <name> [--host localhost] [--port 1883]
   ```
4. To watch what the server sees without running it: `mosquitto_sub -t 'sushi/#' -v`.

## Adding a scenario

Copy an existing YAML in `scenarios/`. Fields: `store_id`, `loop_seconds`, `inject_every_s`,
`classes` (weighted), `cameras` (`id`, `role`, `s_start`, `s_end` on the virtual loop),
`pickups` (`after_s`, `table_id`), `occlusions` (`camera_id`, `at_s`, `duration_s`).
