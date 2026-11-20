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
* Position: several times a second it publishes where it is (x, y, heading, speed,
  battery) and what it is doing. That is what the console draws.
* Like a real one it speeds up and slows down (no jumps to full speed), spends battery
  per metre walked, and charges at its base when it is below ROBOT_CHARGE_BELOW.

The robot is a state machine moved by ``tick(dt)``, so it is tested without clocks.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import threading
import time
import urllib.request
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
    """The physical world as the robot meets it: its camera and its arm (simulated)."""

    def __init__(self, url: str, photos_url: str) -> None:
        self._url = url
        self._photos_url = photos_url  # where the robot serves its own photos

    def photo(self, poi: str) -> str:
        with urllib.request.urlopen(f"{self._url}/camera/{poi.lower()}", timeout=5) as response:
            return f"{self._photos_url}/fotos/{json.load(response)['photo']}"

    def valve_closed(self) -> bool:
        with urllib.request.urlopen(f"{self._url}/valves/xv-104", timeout=5) as response:
            return bool(json.load(response)["closed"])

    def close_valve(self) -> None:
        request = urllib.request.Request(f"{self._url}/valves/xv-104/close", method="POST")
        urllib.request.urlopen(request, timeout=5).close()


PHOTO_NAME = re.compile(r"^[a-z0-9_.-]+\.jpe?g$")


def serve_photos(world_url: str, plant: Plant, port: int) -> ThreadingHTTPServer:
    """The robot's own little server, like a real one has: the photos it took (with
    their credits) and the map it walks. It is what the rest of the plant network sees;
    the simulator behind it stays on the field network."""

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: bytes, kind: str = "application/json") -> None:
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 (http.server naming)
            if self.path == "/planta":
                self._send(200, json.dumps(plant.raw).encode())
                return
            name = self.path.removeprefix("/fotos").lstrip("/")
            if not self.path.startswith("/fotos") or (name and not PHOTO_NAME.match(name)):
                self._send(404, b'{"error": "not found"}')
                return
            try:
                with urllib.request.urlopen(f"{world_url}/fotos{'/' + name if name else ''}", timeout=5) as r:
                    self._send(200, r.read(), r.headers.get("Content-Type", "application/octet-stream"))
            except OSError:
                self._send(404, b'{"error": "not found"}')

        def log_message(self, *args) -> None:
            pass

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


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
        accel: float = 0.6,  # m/s²
        drain_per_m: float = 0.05,  # % of battery per metre
        charge_rate: float = 3.0,  # % per second at the base
        charge_below: float = 60.0,  # % under which it stays at the base to charge
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
        self._accel = accel
        self._drain = drain_per_m
        self._charge_rate = charge_rate
        self._charge_below = charge_below
        self.v = 0.0  # current speed, m/s
        self.battery = 92.0

    # -- inputs ------------------------------------------------------------------

    def on_order(self, topic: str, order: Message) -> None:
        """Called from the MQTT thread: only queued, the robot acts on its next tick."""
        if order.value != "cerrar":
            log.warning("unknown order for XV-104: %r", order.value)
            return
        self._orders.put(order.value)

    # -- the state machine -------------------------------------------------------

    def tick(self, dt: float) -> None:
        self._take_orders()  # an order interrupts a stop or a charge
        if self.state == "cargando":
            self.battery = min(100.0, self.battery + self._charge_rate * dt)
            if self.battery < 95.0:
                self.publish_pose()
                return
            if self._mission is None:
                self._carry_on()
        if self._wait > 0:
            self._wait -= dt
            if self._wait <= 0:
                done, self._after_wait = self._after_wait, None
                if done:
                    done()
            self.publish_pose()
            return

        target = self._mission or self._next
        remaining = self._distance_to(target) if target else float("inf")
        # speed up to cruise speed, and brake in time to stop on the point
        braking = (2 * self._accel * max(remaining, 0.0)) ** 0.5
        corner = (2 * self._accel * self._route.to_corner(self.s, self._direction)) ** 0.5 + self._speed / 3
        self.v = min(self._speed, self.v + self._accel * dt, max(min(braking, corner), 0.3))
        step = self.v * dt
        if target is not None and step >= remaining - ARRIVED:
            step = max(remaining, 0.0)
            self.s = target.s
            self.v = 0.0
            self._spend(step)
            self._arrive(target)
        else:
            self.s = (self.s + self._direction * step) % self._route.length
            self._spend(step)
        self.publish_pose()

    def _spend(self, metres: float) -> None:
        self.battery = max(0.0, self.battery - self._drain * metres)

    def _take_orders(self) -> None:
        try:
            self._orders.get_nowait()
        except queue.Empty:
            return
        if self._mission is not None:
            return  # already on its way
        self._wait, self._after_wait = 0.0, None  # leave the stop it was at
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
        if stop.action == "carga":
            if self.battery < self._charge_below:
                self.state = "cargando"
                log.info("battery at %.0f %%: charging", self.battery)
            else:
                self._carry_on()
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
        captured = uns.now_iso()
        extra = {"poi": device, "image_url": self._world.photo(device), "captured_at": captured}
        self._publish(uns.ROBOT_PHOTO, Message(image, ts=captured, source="robot-01", extra=extra), False)
        # The last photo of each point stays in the UNS (retained), so whoever arrives
        # late (a console, a person) can still see what the robot saw there.
        last = Message(image, ts=captured, source="robot-01", extra=extra)
        self._publish(f"{uns.ROBOT_LAST_PHOTO}/{device.lower()}", last, True)

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
            "velocidad": round(self.v, 2),
            "bateria": round(self.battery, 1),
            "destino": target.device if target else None,
        }
        self._publish(uns.ROBOT_POSE, Message(self.state, source="robot-01", extra=extra), True)


def main() -> int:
    from .bus import Bus, setup_logging, stop_on_signal

    setup_logging()
    bus = Bus("robot-01")
    world_url = os.environ.get("WORLD_URL", "http://localhost:8080")
    port = int(os.environ.get("ROBOT_HTTP_PORT", "8081"))
    plant = load_plant()
    serve_photos(world_url, plant, port)
    robot = Robot(
        HttpWorld(world_url, os.environ.get("ROBOT_URL", f"http://localhost:{port}")),
        plant,
        publish=lambda topic, message, retain: bus.publish(topic, message, retain=retain),
        speed=float(os.environ.get("ROBOT_SPEED", "1.5")),
        dwell_s=float(os.environ.get("ROBOT_DWELL_S", "3")),
        action_s=float(os.environ.get("ROBOT_ACTION_S", "4")),
        accel=float(os.environ.get("ROBOT_ACCEL", "0.6")),
        charge_rate=float(os.environ.get("ROBOT_CHARGE_RATE", "3")),
        charge_below=float(os.environ.get("ROBOT_CHARGE_BELOW", "60")),
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
