"""The inspection robot, simulated: it walks the plant and does what a real one does.

* Round: it follows the route of ``config/planta.yaml`` in a loop. At a ``foto`` stop
  it photographs the point of interest (gauge PI-104, extinguisher EXT-12) and the
  photo goes to the UNS with its POI and a link to the image, which is what the
  inspector (poi-vision-inspector) listens for. At a ``valvula`` stop it checks the
  position of XV-104 and reports it: a manual valve has no position switch, so the
  robot is the only one that knows.
* Orders: when the rules ask for ``cerrar`` on XV-104, it leaves the round, goes to the
  valve by the shorter way along the route, closes it with the arm, reports back and
  carries on. Orders are not retained, so a restarted robot never replays an old one.
* Position: several times a second it publishes where it is (x, y, heading) and what
  it is doing. That is what the console draws.

The robot is a state machine moved by ``tick(dt)``, so it is tested without clocks.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import time
import urllib.request
from collections.abc import Callable
from typing import Protocol

from . import uns
from .plant import Plant, Stop, load_plant
from .uns import Message

log = logging.getLogger("robot")

ARRIVED = 1e-6


class World(Protocol):
    def photo(self, poi: str) -> str: ...  # URL of the photo the camera just took
    def valve_closed(self) -> bool: ...
    def close_valve(self) -> None: ...


class HttpWorld:
    def __init__(self, url: str) -> None:
        self._url = url

    def photo(self, poi: str) -> str:
        with urllib.request.urlopen(f"{self._url}/camera/{poi.lower()}", timeout=5) as response:
            return f"{self._url}/fotos/{json.load(response)['photo']}"

    def valve_closed(self) -> bool:
        with urllib.request.urlopen(f"{self._url}/valves/xv-104", timeout=5) as response:
            return bool(json.load(response)["closed"])

    def close_valve(self) -> None:
        request = urllib.request.Request(f"{self._url}/valves/xv-104/close", method="POST")
        urllib.request.urlopen(request, timeout=5).close()


Publish = Callable[[str, Message, bool], None]


class Robot:
    def __init__(
        self,
        world: World,
        plant: Plant,
        publish: Publish,
        speed: float = 1.5,  # m/s
        dwell_s: float = 3.0,  # time at a stop
        action_s: float = 4.0,  # time to close the valve
    ) -> None:
        self._world = world
        self._plant = plant
        self._route = plant.route
        self._publish = publish
        self._speed = speed
        self._dwell_s = dwell_s
        self._action_s = action_s
        self._orders: queue.Queue[str] = queue.Queue()

        self.s = 0.0  # position along the route; the first point is the charging base
        self.state = "en-ruta"
        self._next = self._first_stop_ahead()
        self._wait = 0.0  # seconds left at the current stop
        self._after_wait: Callable[[], None] | None = None
        self._mission: Stop | None = None
        self._direction = 1  # +1 forward along the route, -1 backward (missions only)
        self._photos = 410

    # -- inputs ------------------------------------------------------------------

    def on_order(self, topic: str, order: Message) -> None:
        """Called from the MQTT thread: only queued, the robot acts on its next tick."""
        if order.value != "cerrar":
            log.warning("unknown order for XV-104: %r", order.value)
            return
        self._orders.put(order.value)

    # -- the state machine -------------------------------------------------------

    def tick(self, dt: float) -> None:
        if self._wait > 0:
            self._wait -= dt
            if self._wait <= 0:
                done, self._after_wait = self._after_wait, None
                if done:
                    done()
            self.publish_pose()
            return

        self._take_orders()
        target = self._mission or self._next
        if target is None:  # a route without stops: just walk
            self.s = (self.s + self._speed * dt) % self._route.length
        else:
            remaining = self._distance_to(target)
            step = self._speed * dt
            if step >= remaining - ARRIVED:
                self.s = target.s
                self._arrive(target)
            else:
                self.s = (self.s + self._direction * step) % self._route.length
        self.publish_pose()

    def _take_orders(self) -> None:
        try:
            self._orders.get_nowait()
        except queue.Empty:
            return
        if self._mission is not None:
            return  # already on its way
        self._mission = self._plant.stop_for("XV-104")
        forward = self._route.ahead(self.s, self._mission.s)
        backward = self._route.ahead(self._mission.s, self.s)
        self._direction = 1 if forward <= backward else -1
        self.state = "en-mision"
        self._report_mission("en-camino")

    def _distance_to(self, stop: Stop) -> float:
        if self._direction > 0:
            return self._route.ahead(self.s, stop.s)
        return self._route.ahead(stop.s, self.s)

    def _arrive(self, stop: Stop) -> None:
        if self._mission is stop:
            self.state = "actuando"
            self._hold(self._action_s, self._finish_mission)
            return
        if stop.action == "foto":
            self.state = "inspeccionando"
            self._take_photo(stop.device)
        else:
            self.state = "comprobando"
            self._report_valve()
        self._hold(self._dwell_s, self._carry_on)

    def _hold(self, seconds: float, then: Callable[[], None]) -> None:
        self._wait = seconds
        self._after_wait = then
        if seconds <= 0:
            self._wait = 0
            self._after_wait = None
            then()

    def _finish_mission(self) -> None:
        self._world.close_valve()
        self._report_valve()
        self._report_mission("completada")
        self._mission = None
        self._direction = 1
        self._carry_on()

    def _carry_on(self) -> None:
        self.state = "en-ruta"
        self._next = self._first_stop_ahead()

    def _first_stop_ahead(self) -> Stop | None:
        stops = self._plant.stops
        if not stops:
            return None

        def ahead(stop: Stop) -> float:
            d = self._route.ahead(self.s, stop.s)
            return d if d > 1e-3 else self._route.length  # the stop it is standing on is the last one

        return min(stops, key=ahead)

    # -- outputs -----------------------------------------------------------------

    def _take_photo(self, device: str) -> None:
        self._photos += 1
        image = f"IMG_{self._photos:04d}"
        extra = {"poi": device, "image_url": self._world.photo(device)}
        self._publish(uns.ROBOT_PHOTO, Message(image, source="robot-01", extra=extra), False)

    def _report_valve(self) -> None:
        state = "cerrada" if self._world.valve_closed() else "abierta"
        self._publish(uns.XV_104_STATE, Message(state, source="robot-01"), True)

    def _report_mission(self, state: str) -> None:
        log.info("mission to XV-104: %s", state)
        extra = {"target": "XV-104", "action": "cerrar"}
        self._publish(uns.ROBOT_MISSION, Message(state, source="robot-01", extra=extra), True)

    def publish_pose(self) -> None:
        (x, y), heading = self._route.at(self.s)
        if self._direction < 0:
            heading = (heading + 180) % 360
        target = self._mission or (self._next if self.state == "en-ruta" else None)
        extra = {
            "x": round(x, 2),
            "y": round(y, 2),
            "rumbo": round(heading, 1),
            "destino": target.device if target else None,
        }
        self._publish(uns.ROBOT_POSE, Message(self.state, source="robot-01", extra=extra), True)


def main() -> int:
    from .bus import Bus, setup_logging, stop_on_signal

    setup_logging()
    bus = Bus("robot-01")
    robot = Robot(
        HttpWorld(os.environ.get("WORLD_URL", "http://localhost:8080")),
        load_plant(),
        publish=lambda topic, message, retain: bus.publish(topic, message, retain=retain),
        speed=float(os.environ.get("ROBOT_SPEED", "1.5")),
        dwell_s=float(os.environ.get("ROBOT_DWELL_S", "3")),
        action_s=float(os.environ.get("ROBOT_ACTION_S", "4")),
    )
    bus.subscribe(uns.XV_104_ORDER, robot.on_order, retained=False)
    stop = stop_on_signal()
    bus.start()
    rate_hz = float(os.environ.get("ROBOT_RATE_HZ", "5"))
    last = time.monotonic()
    while not stop.wait(1 / rate_hz):
        now = time.monotonic()
        try:
            robot.tick(now - last)
        except OSError as exc:
            log.warning("world not reachable, waiting: %s", exc)
        last = now
    bus.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
