# Map schema

Produced by the frontend map editor, stored versioned in `backend` (MySQL `maps` table),
fetched by `server` on boot and on every `sushi/<store>/config/map` signal.

```yaml
version: 7                     # assigned by backend on save
store_id: store-001
floorplan:
  image_id: fp_a1b2            # uploaded via POST /api/floorplans
  width_px: 1920
  height_px: 1080

track:
  # Clicked on the floor-plan image, in image pixels, ordered along travel direction.
  # Rendered as a Catmull-Rom spline, sampled internally to a fine polyline for arc-length.
  path_px:
    - [120, 340]
    - [430, 332]
    - [760, 350]
    - [1180, 520]
    - [430, 720]
    - [120, 540]
  closed: true
  # OPTIONAL cold-start seed only. Eyeball with a stopwatch. Not used once the
  # per-segment travel-time estimator has enough matched plates.
  approx_loop_seconds: 108

cameras:
  - id: cam_kitchen
    role: source              # source = kitchen output; counts injected plates, no billing
    counting_point_px: [150, 430]
    view_span_px: [[120, 400], [230, 470]]

  - id: cam_t01
    role: table
    table_id: T01
    view_span_px: [[430, 332], [760, 350]]   # two points ON the path = upstream/downstream edge
    counting_point_px: [470, 335]            # on the path, inside the span; default = upstream edge
    pickup_zone_px: [[440, 334], [740, 349]] # optional; with 1:1 camera:table, defaults to the whole span
```

## Derived by `server` (never authored)

- `path_px` → fine polyline → cumulative length → normalised arc-length `s in [0, 1)`.
- Each `view_span_px` pair → `[s_start, s_end]` by projecting onto the polyline.
- `counting_point_px` → `s_c`.
- Counting lines ordered around the loop → **segments** between consecutive lines.
- Adjacent camera spans compared:
  - `span_B.start < span_A.end` → **overlap region** → simultaneous-detection de-dup.
  - `span_B.start > span_A.end` → **gap** → dead-reckon across it; expected segment travel
    time from the estimator (seeded by `approx_loop_seconds * gap_len / loop_len`).
- Per-segment table membership (with `role: table`, 1:1) → which table(s) a pickup in that
  segment is attributed to.

## Editor validation (before save)

- Every camera has a `view_span_px` (2 points, both near the path) and a `counting_point_px` on the path within the span.
- No duplicate camera `id`.
- Exactly the camera ids that are publishing on the broker are offered (warn on mismatch).
- Coverage overlay: shade the path green (≥2 cameras) / yellow (1) / red (0). Warn on any
  red segment or any `role: table` whose pickup zone is covered by only its own camera.
