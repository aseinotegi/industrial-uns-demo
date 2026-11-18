import json

import pytest

from uns_demo import uns
from uns_demo.uns import Message


def test_namespace_follows_site_area_device_value():
    assert uns.PT_104 == "planta1/exterior/pt-104/presion"
    assert uns.PI_104 == "planta1/exterior/pi-104/presion"
    assert uns.XV_104_ORDER.startswith("planta1/exterior/xv-104/")


@pytest.mark.parametrize("level", ["Planta1", "pt 104", "", "-x", "a/b", "presión"])
def test_rejects_invalid_levels(level):
    with pytest.raises(ValueError):
        uns.path("planta1", level)


def test_message_round_trip_keeps_extra_fields():
    original = Message(6.8, unit="bar", source="vision", extra={"confidence": 0.98, "image": "IMG_0412"})
    copy = Message.from_json(original.to_json())
    assert (copy.value, copy.unit, copy.source, copy.quality) == (6.8, "bar", "vision", "good")
    assert copy.extra == {"confidence": 0.98, "image": "IMG_0412"}
    assert copy.ts == original.ts


def test_gateway_payload_is_a_valid_message():
    # what plc-mqtt-gateway publishes: no source, no quality, timestamp with offset
    message = Message.from_json('{"value":6.71,"ts":"2026-11-18T11:07:25.329+00:00","unit":"bar"}')
    assert message.number == 6.71 and message.quality == "good"
    assert uns.parse_ts(message.ts).tzinfo is not None


@pytest.mark.parametrize("raw", ["[]", '{"unit": "bar"}', "not json"])
def test_rejects_messages_without_value(raw):
    with pytest.raises(ValueError):
        Message.from_json(raw)


def test_number_treats_booleans_as_0_1_and_text_as_none():
    assert Message(True).number == 1.0
    assert Message("cerrar").number is None


def test_json_is_compact_and_utf8():
    body = Message("cerrada", source="robot-01").to_json()
    assert " " not in body.replace("robot-01", "")
    assert json.loads(body)["value"] == "cerrada"
