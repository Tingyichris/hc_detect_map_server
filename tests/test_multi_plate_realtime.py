from hcdmap_server.config import Config
from hcdmap_server.fusion.engine import FusionEngine
from hcdmap_server.mqtt.schema import PlateEvent

from test_pickup_wiring import _map


def event(camera_id, kind, plate_id, plate_class, ts, handoff_id=None):
    payload = {
        "camera_id": camera_id,
        "event": kind,
        "plate_id": plate_id,
        "class": plate_class,
        "ts": ts,
    }
    if handoff_id:
        payload["handoff_id"] = handoff_id
    return PlateEvent.model_validate(payload)


def test_multiple_plates_remain_independent_and_provisional_catch_revokes():
    engine = FusionEngine(Config(), _map())
    engine.on_event(event("cam_a", "entry", 1, "red", 10.0))
    engine.on_event(event("cam_a", "entry", 2, "gold", 10.4))

    assert len(engine.plates) == 2
    first = engine._binding[("cam_a", 1)]
    second = engine._binding[("cam_a", 2)]
    assert first != second

    engine.on_event(event("cam_a", "catch_suspected", 1, "red", 10.8))
    assert engine.plates[first].status == "suspected"
    assert engine.plates[second].status == "tracked"

    engine.on_event(event("cam_a", "catch_revoked", 1, "red", 10.9))
    assert engine.plates[first].status == "tracked"


def test_stale_same_colour_handoff_does_not_consume_next_plate():
    cfg = Config()
    engine = FusionEngine(cfg, _map())
    engine.on_event(event("cam_a", "entry", 1, "red", 1.0))
    engine.on_event(event("cam_a", "exit", 1, "red", 2.0))
    stale_gid = engine._handoffs["cam_b"][0]

    # This downstream event is far outside the learned/seeded arrival window.
    engine.on_event(event("cam_b", "entry", 20, "red", 50.0))
    new_gid = engine._binding[("cam_b", 20)]

    assert new_gid != stale_gid
    assert stale_gid in engine._handoffs["cam_b"]


def test_camera_motion_uses_segment_estimate():
    engine = FusionEngine(Config(), _map())
    expected = engine.travel.get("cam_a", "cam_b").estimate()
    engine.on_event(event("cam_a", "entry", 1, "red", 10.0))
    gid = engine._binding[("cam_a", 1)]
    assert engine.plates[gid].motion_duration_s == expected


def test_downstream_entry_is_hidden_until_handoff_window_expires():
    engine = FusionEngine(Config(), _map())
    engine.on_event(event("cam_b", "entry", 20, "red", 10.0))
    gid = engine._binding[("cam_b", 20)]

    assert engine.plates[gid].pending_until == 12.0
    engine.tick(11.9)
    assert engine.plates[gid].pending_until == 12.0

    engine.tick(12.0)
    assert engine.plates[gid].pending_until == 0.0


def test_same_plate_records_crossing_again_after_full_loop():
    engine = FusionEngine(Config(), _map())
    handoff_id = "plate-global-123"

    engine.on_event(event("cam_a", "entry", 1, "red", 10.0, handoff_id))
    gid = engine._binding[("cam_a", 1)]
    engine.on_event(event("cam_a", "exit", 1, "red", 11.0, handoff_id))
    engine.on_event(event("cam_b", "entry", 2, "red", 20.0, handoff_id))
    engine.on_event(event("cam_b", "exit", 2, "red", 21.0, handoff_id))
    engine.on_event(event("cam_a", "entry", 3, "red", 30.0, handoff_id))
    engine.on_event(event("cam_a", "exit", 3, "red", 31.0, handoff_id))

    assert engine._handoff_bindings[handoff_id] == gid
    assert engine.plates[gid].crossing_counts["cam_a"] == 2
    assert len(engine.matcher.log_for("cam_a").crossings) == 2
