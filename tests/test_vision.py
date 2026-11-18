from uns_demo.vision import GaugeModel, Reading, reading_message


def test_good_photos_read_close_to_the_truth_with_high_confidence():
    model = GaugeModel(seed=7, bad_photo_rate=0)
    readings = [model.read(6.8) for _ in range(200)]
    assert all(r.trusted and 0.94 <= r.confidence <= 0.99 for r in readings)
    assert all(abs(r.value - 6.8) <= 0.2 for r in readings)


def test_bad_photos_are_flagged_not_hidden():
    model = GaugeModel(seed=7, bad_photo_rate=1)
    reading = model.read(4.2)
    assert not reading.trusted
    message = reading_message(reading, "IMG_0413")
    assert message.quality == "uncertain"  # published, so the dashboard can show it, but rules skip it


def test_reading_message_carries_the_evidence():
    message = reading_message(Reading("manometro", 0.98, 6.8), "IMG_0412")
    assert (message.value, message.unit, message.source, message.quality) == (6.8, "bar", "vision", "good")
    assert message.extra == {"confidence": 0.98, "label": "manometro", "image": "IMG_0412"}
