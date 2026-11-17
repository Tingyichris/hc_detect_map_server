import json

from hcdmap_server.config import Config
from hcdmap_server.mqtt.consumer import Consumer
from hcdmap_server.mqtt.schema import CameraStatus, PlateEvent


def test_v2_event_topic_contains_pi_and_local_camera_identity():
    message = Consumer(Config())._parse(
        "sushi/pi-01/events/cam-1",
        json.dumps({"event": "entry", "plate_id": 7, "class": "red"}).encode(),
    )
    assert isinstance(message, PlateEvent)
    assert message.pi_id == "pi-01"
    assert message.camera_id == "cam-1"
    assert message.source_id == "pi-01/cam-1"


def test_v2_status_topic_contains_source_identity():
    message = Consumer(Config())._parse(
        "sushi/pi-02/status/cam-1", b'{"state":"online"}'
    )
    assert isinstance(message, CameraStatus)
    assert message.source_id == "pi-02/cam-1"


def test_legacy_topic_remains_supported():
    message = Consumer(Config())._parse(
        "sushi/events/cam-1",
        json.dumps({"event": "entry", "plate_id": 7, "class": "red"}).encode(),
    )
    assert isinstance(message, PlateEvent)
    assert message.source_id == "cam-1"
