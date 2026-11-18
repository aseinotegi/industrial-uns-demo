"""Vision service on the edge PC: turns the robot's photo of PI-104 into a reading.

In production this is a detector (is it a gauge, and where) followed by a model
that reads the needle on the crop. Here the "photo" is the gauge's true value
fetched from the simulated world, and the model is replaced by its typical
behaviour: a confidence, a small reading error and, now and then, a bad photo
(glare, blur) that comes back as ``quality: uncertain`` instead of a number to trust.

The reading is published to the UNS like any other value. Nobody downstream needs
to know it came from a camera.
"""

from __future__ import annotations

import json
import logging
import os
import random
import urllib.request
from dataclasses import dataclass

from . import uns
from .uns import Message

log = logging.getLogger("vision")

MIN_CONFIDENCE = 0.85


@dataclass
class Reading:
    label: str
    confidence: float
    value: float

    @property
    def trusted(self) -> bool:
        return self.confidence >= MIN_CONFIDENCE


class GaugeModel:
    """Stand-in for detector + reader, with realistic misbehaviour."""

    def __init__(self, seed: int | None = None, bad_photo_rate: float = 0.05) -> None:
        self._rng = random.Random(seed)
        self._bad_photo_rate = bad_photo_rate

    def read(self, true_value: float) -> Reading:
        if self._rng.random() < self._bad_photo_rate:
            return Reading("manometro", round(self._rng.uniform(0.40, 0.75), 2), round(true_value + 1.5, 1))
        confidence = round(self._rng.uniform(0.94, 0.99), 2)
        return Reading("manometro", confidence, round(true_value + self._rng.gauss(0, 0.03), 1))


def reading_message(reading: Reading, image: str) -> Message:
    return Message(
        value=reading.value,
        unit="bar",
        source="vision",
        quality="good" if reading.trusted else "uncertain",
        extra={"confidence": reading.confidence, "label": reading.label, "image": image},
    )


def fetch_gauge(world_url: str) -> float:
    with urllib.request.urlopen(f"{world_url}/gauges/pi-104", timeout=5) as response:
        return float(json.load(response)["pressure"])


def main() -> int:
    from .bus import Bus, setup_logging, stop_on_signal

    setup_logging()
    world_url = os.environ.get("WORLD_URL", "http://localhost:8080")
    model = GaugeModel()
    bus = Bus("vision")

    def on_photo(topic: str, photo: Message) -> None:
        if photo.extra.get("point") != "PI-104":
            return
        reading = model.read(fetch_gauge(world_url))
        bus.publish(uns.PI_104, reading_message(reading, str(photo.value)))
        log.info(
            "%s → %s %.2f, reading %.1f bar", photo.value, reading.label, reading.confidence, reading.value
        )

    bus.subscribe(uns.ROBOT_PHOTO, on_photo)
    stop = stop_on_signal()
    bus.start()
    stop.wait()
    bus.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
