"""The inspection robot, simulated.

* Rounds: every ``round_s`` it stops at each point of interest (gauge PI-104 and
  extinguisher EXT-12) and takes a photo. The photo goes to the UNS with its POI and
  a link to the image, which is what the inspector (poi-vision-inspector) listens for.
  Then it checks the position of XV-104 and reports it: a manual valve has no
  position switch, so the robot is the only one that knows.
* Orders: when the rules ask for ``cerrar`` on XV-104, it goes there, closes it with
  the arm and reports back. Orders are not retained, so a restarted robot never
  replays an old one.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import urllib.request
from collections.abc import Callable
from typing import Protocol

from . import uns
from .uns import Message

log = logging.getLogger("robot")


ROUND = ("pi-104", "ext-12")  # points of interest, in the order the robot visits them


class World(Protocol):
    def photo(self, poi: str) -> str: ...  # URL of the photo the camera just took
    def valve_closed(self) -> bool: ...
    def close_valve(self) -> None: ...


class HttpWorld:
    def __init__(self, url: str) -> None:
        self._url = url

    def photo(self, poi: str) -> str:
        with urllib.request.urlopen(f"{self._url}/camera/{poi}", timeout=5) as response:
            return f"{self._url}/fotos/{json.load(response)['photo']}"

    def valve_closed(self) -> bool:
        with urllib.request.urlopen(f"{self._url}/valves/xv-104", timeout=5) as response:
            return bool(json.load(response)["closed"])

    def close_valve(self) -> None:
        request = urllib.request.Request(f"{self._url}/valves/xv-104/close", method="POST")
        urllib.request.urlopen(request, timeout=5).close()


Publish = Callable[[str, Message, bool], None]


class Robot:
    def __init__(self, world: World, publish: Publish, travel_s: float = 10.0, sleep=None) -> None:
        self._world = world
        self._publish = publish
        self._travel_s = travel_s
        self._sleep = sleep or threading.Event().wait
        self._photos = 410
        self._busy = threading.Lock()

    def inspection_round(self) -> None:
        if not self._busy.acquire(blocking=False):
            return  # on a mission; the round waits
        try:
            for poi in ROUND:
                self._photos += 1
                image = f"IMG_{self._photos:04d}"
                extra = {"poi": poi.upper(), "image_url": self._world.photo(poi)}
                self._publish(uns.ROBOT_PHOTO, Message(image, source="robot-01", extra=extra), False)
            self._report_valve()
        finally:
            self._busy.release()

    def on_order(self, topic: str, order: Message) -> None:
        if order.value != "cerrar":
            log.warning("unknown order for XV-104: %r", order.value)
            return
        with self._busy:
            self._mission("en-camino", "XV-104")
            self._sleep(self._travel_s)
            self._world.close_valve()
            self._report_valve()
            self._mission("completada", "XV-104")

    def _report_valve(self) -> None:
        state = "cerrada" if self._world.valve_closed() else "abierta"
        self._publish(uns.XV_104_STATE, Message(state), True)

    def _mission(self, state: str, target: str) -> None:
        log.info("mission to %s: %s", target, state)
        self._publish(uns.ROBOT_MISSION, Message(state, extra={"target": target, "action": "cerrar"}), True)


def main() -> int:
    from .bus import Bus, setup_logging, stop_on_signal

    setup_logging()
    bus = Bus("robot-01")
    robot = Robot(
        HttpWorld(os.environ.get("WORLD_URL", "http://localhost:8080")),
        publish=lambda topic, message, retain: bus.publish(topic, message, retain=retain),
        travel_s=float(os.environ.get("ROBOT_TRAVEL_S", "10")),
    )
    # Orders run in their own thread so the MQTT loop keeps flowing during the mission.
    bus.subscribe(uns.XV_104_ORDER, lambda t, m: threading.Thread(target=robot.on_order, args=(t, m)).start())
    stop = stop_on_signal()
    bus.start()
    round_s = float(os.environ.get("ROBOT_ROUND_S", "15"))
    while not stop.wait(round_s):
        try:
            robot.inspection_round()
        except OSError as exc:
            log.warning("round skipped, world not reachable: %s", exc)
    bus.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
