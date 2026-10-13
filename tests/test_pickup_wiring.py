from hcdmap_server.__main__ import _pickup_body
from hcdmap_server.config import Config
from hcdmap_server.fusion.engine import FusionEngine
from hcdmap_server.fusion.pickup import detect
from hcdmap_server.map.model import TrackMap
from hcdmap_server.mqtt.schema import PlateEvent


def _map() -> TrackMap:
    return TrackMap.from_dict(
        {
            "version": 1,
            "store_id": "store-001",
            "floorplan": {"image_id": "fp_test", "width_px": 100, "height_px": 100},
            "track": {
                "path_px": [[0, 0], [100, 0], [100, 100], [0, 100]],
                "closed": True,
                "approx_loop_seconds": 4,
            },
            "cameras": [
                {
                    "id": "cam_a",
                    "role": "table",
                    "table_id": "T01",
                    "view_span_px": [[0, 0], [100, 0]],
                    "counting_point_px": [50, 0],
                },
                {
                    "id": "cam_b",
                    "role": "table",
                    "table_id": "T02",
                    "view_span_px": [[100, 100], [0, 100]],
                    "counting_point_px": [50, 100],
                },
            ],
        }
    )


def _event(kind: str, *, ts: float) -> PlateEvent:
    return PlateEvent.model_validate(
        {
            "camera_id": "cam_a",
            "ts": ts,
            "event": kind,
            "plate_id": 7,
            "class": "red",
        }
    )


def test_tick_returns_overdue_pickup_candidate_once():
    engine = FusionEngine(Config(), _map())
    engine.on_event(_event("entry", ts=10.0))
    engine.on_event(_event("exit", ts=11.0))

    first = engine.tick(20.0)
    second = engine.tick(20.5)

    assert len(first) == 1
    assert first[0].segment == ("cam_a", "cam_b")
    assert first[0].crossing.gid == "p_000001"
    assert second == []


def test_offbelt_candidate_becomes_pickup_body_for_backend():
    cfg = Config()
    tm = _map()
    engine = FusionEngine(cfg, tm)
    engine.on_event(_event("entry", ts=10.0))
    engine.on_event(_event("catch", ts=11.0))

    [uc] = engine.tick(20.0)
    pickup = detect(
        uc,
        tm,
        camera_states={},
        offbelt_gids=engine.offbelt_gids,
        coldstart_done=False,
    )
    body = _pickup_body(cfg, pickup)

    assert pickup.method == "offbelt_confirmed"
    assert body["event_id"]
    assert body["store_id"] == "store-001"
    assert body["gid"] == "p_000001"
    assert body["table_id"] == "T01"
    assert body["camera_id"] == "cam_a"
    assert body["class"] == "red"
    assert "plate_class" not in body
    assert body["integrity"] == "estimated"
