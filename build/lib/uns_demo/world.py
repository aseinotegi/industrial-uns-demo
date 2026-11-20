"""The physical plant, simulated: what in real life is steel, not software.

It owns the truth that the other services can only observe:

* the line pressure, measured twice: by transmitter PT-104 (wired to the PLC) and
  by local gauge PI-104 (a dial with no signal; only a photo can read it);
* manual valve XV-104, which only the robot can close;
* fire extinguisher EXT-12, on the robot's round too.

It exposes them the way a real plant would:

* a Siemens S7 PLC (DB1) for the gateway to read, on port 1102;
* a tiny HTTP API standing in for the physical world: the robot's camera "looks"
  at a point of interest (``GET /camera/pi-104``) and gets one of the dataset photos,
  and its arm turns the valve (``POST /valves/xv-104/close``).

Both come from ``dataset/``, so the demo is repeatable and easy to change:

* ``dataset/plc/linea-104.csv``: the line pressure over one cycle while XV-104 is
  open (an upstream disturbance from 4.2 up to 6.8 bar and back). Closing the valve
  brings it down to normal; near the end of the cycle an operator reopens it.
* ``dataset/fotos.csv``: the photos and their true values. The camera picks the
  photo of PI-104 closest to what the dial shows right now, so the photo and the PLC
  always tell the same story. Now and then it is the blurred or glared one.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import json
import logging
import math
import os
import random
import struct
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

log = logging.getLogger("world")

NORMAL_BAR = 4.2
PLC_ALARM_BAR = 6.0  # the PLC's own high-pressure bit


def default_dataset() -> Path:
    if os.environ.get("DATASET_DIR"):
        return Path(os.environ["DATASET_DIR"])
    return Path(__file__).resolve().parents[2] / "dataset"


# -- the line ------------------------------------------------------------------


class Profile:
    """Line pressure over one cycle with the valve open, from a CSV (t_s, bar)."""

    def __init__(self, points: list[tuple[float, float]]) -> None:
        if len(points) < 2:
            raise ValueError("a pressure profile needs at least two points")
        self._t = [t for t, _ in points]
        self._bar = [bar for _, bar in points]

    @classmethod
    def load(cls, path: Path, time_scale: float = 1.0) -> Profile:
        with path.open(encoding="utf-8") as f:
            points = [(float(row["t_s"]) * time_scale, float(row["bar"])) for row in csv.DictReader(f)]
        return cls(sorted(points))

    @property
    def cycle_s(self) -> float:
        return self._t[-1]

    @property
    def rise_at(self) -> float:
        """When the disturbance starts: the last point before the pressure leaves its first value."""
        for i, bar in enumerate(self._bar):
            if abs(bar - self._bar[0]) > 0.05:
                return self._t[max(i - 1, 0)]
        return 0.0

    def points(self) -> list[list[float]]:
        return [[t, bar] for t, bar in zip(self._t, self._bar, strict=True)]

    def at(self, t: float) -> float:
        """Linear interpolation between the points of the profile."""
        i = bisect.bisect_right(self._t, t)
        if i == 0:
            return self._bar[0]
        if i == len(self._t):
            return self._bar[-1]
        t0, t1 = self._t[i - 1], self._t[i]
        b0, b1 = self._bar[i - 1], self._bar[i]
        return b0 + (b1 - b0) * (t - t0) / (t1 - t0)


@dataclass
class Scenario:
    profile: Profile
    reopen_at_s: float = 170.0  # operator reopens XV-104, ready for the next cycle
    decay_s: float = 6.0  # time constant once the valve is closed
    follow_s: float = 1.0  # how closely the line follows the profile

    @classmethod
    def from_env(cls, dataset: Path | None = None) -> Scenario:
        scale = float(os.environ.get("WORLD_TIME_SCALE", "1"))  # < 1 runs the story faster
        profile = Profile.load((dataset or default_dataset()) / "plc" / "linea-104.csv", scale)
        return cls(profile, reopen_at_s=170.0 * scale, decay_s=6.0 * scale, follow_s=1.0 * scale)

    @property
    def cycle_s(self) -> float:
        return self.profile.cycle_s


class Line:
    """Pressure of the line around PT-104 / PI-104 and position of XV-104."""

    def __init__(self, scenario: Scenario, seed: int | None = None) -> None:
        self.scenario = scenario
        self.pressure = NORMAL_BAR
        self.valve_closed = False
        self._rng = random.Random(seed)
        self._lock = threading.Lock()
        self._t = 0.0
        self._offset = 0.0  # moves the cycle when someone asks for a disturbance now

    def phase(self) -> float:
        return (self._t + self._offset) % self.scenario.cycle_s

    def disturb_now(self) -> bool:
        """Jumps the cycle to the start of the disturbance. False while the line is still
        above normal (one disturbance at a time)."""
        sc = self.scenario
        with self._lock:
            if self.pressure > NORMAL_BAR + 0.3:
                return False
            self._offset += (sc.profile.rise_at - self.phase()) % sc.cycle_s
            if self.valve_closed:
                self.valve_closed = False
                log.info("operator reopened XV-104 for the new disturbance")
        log.info("upstream disturbance requested")
        return True

    def step(self, t: float, dt: float) -> None:
        """Advances the physics to time ``t`` (seconds since start)."""
        sc = self.scenario
        self._t = t
        phase = self.phase()
        with self._lock:
            if self.valve_closed and phase >= sc.reopen_at_s:
                self.valve_closed = False
                log.info("operator reopened XV-104")
            if self.valve_closed:
                target, tau = NORMAL_BAR, sc.decay_s
            else:
                target, tau = sc.profile.at(phase), sc.follow_s
            self.pressure += (target - self.pressure) * (1 - math.exp(-dt / tau))

    def close_valve(self) -> bool:
        """Returns False if it was already closed."""
        with self._lock:
            was_open, self.valve_closed = not self.valve_closed, True
        return was_open

    def transmitter(self) -> float:
        """PT-104: 4-20 mA transmitter, small electrical noise."""
        return round(self.pressure + self._rng.gauss(0, 0.01), 3)

    def gauge(self) -> float:
        """PI-104: what the dial shows. Same line, slightly different instrument."""
        return round(self.pressure + 0.08 + self._rng.gauss(0, 0.02), 3)


# -- the camera ----------------------------------------------------------------


@dataclass(frozen=True)
class Photo:
    file: str
    poi: str
    value: str
    condition: str


class Camera:
    """What the robot's camera sees at each point of interest, from the dataset."""

    def __init__(
        self, dataset: Path, seed: int | None = None, trouble_rate: float = 0.25, hold_rounds: int = 8
    ) -> None:
        self.folder = dataset / "fotos"
        with (dataset / "fotos.csv").open(encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        self.photos = [Photo(r["archivo"], r["poi"], r["valor"], r["condicion"]) for r in rows]
        self._by_file = {p.file: p for p in self.photos}
        self._rng = random.Random(seed)
        self._trouble_rate = trouble_rate
        self._hold_rounds = hold_rounds
        self._state: dict[str, tuple[list[Photo], int]] = {}  # poi -> (photos of its state, rounds left)

    def pois(self) -> list[str]:
        return sorted({p.poi for p in self.photos})

    def shoot(self, poi: str, line: Line) -> Photo:
        photos = [p for p in self.photos if p.poi == poi.upper()]
        if not photos:
            raise KeyError(poi)
        if poi.upper() == "PI-104":
            return self._gauge(photos, line.gauge())
        return self._state_photo(poi.upper(), photos)

    def _gauge(self, photos: list[Photo], dial: float) -> Photo:
        nearest = min({float(p.value) for p in photos}, key=lambda v: abs(v - dial))
        same = [p for p in photos if float(p.value) == nearest]
        troubled = [p for p in same if p.condition != "limpia"]
        if troubled and self._rng.random() < self._trouble_rate:
            return self._rng.choice(troubled)
        clean = [p for p in same if p.condition == "limpia"] or same
        return clean[0]

    def _state_photo(self, poi: str, photos: list[Photo]) -> Photo:
        # An extinguisher does not change every round: it is fine for a while, and when
        # something is wrong (missing, low pressure) it stays wrong for some rounds too.
        current, left = self._state.get(poi, ([], 0))
        if left <= 0:
            ok = [p for p in photos if p.value == "OK"]
            wrong = [p for p in photos if p.value != "OK"]
            if ok and (not wrong or self._rng.random() > self._trouble_rate):
                current = ok
            else:
                current = [self._rng.choice(wrong)]
            left = self._hold_rounds
        self._state[poi] = (current, left - 1)
        return self._rng.choice(current)

    def read(self, file: str) -> bytes:
        if file not in self._by_file:  # only files listed in the dataset, never a path
            raise KeyError(file)
        return (self.folder / file).read_bytes()


# -- the PLC -------------------------------------------------------------------

DB_NUMBER = 1
DB_SIZE = 8  # DBD0 REAL pt-104 pressure · DBX4.0 BOOL high pressure


def pack_db(db: bytearray, pressure: float) -> None:
    struct.pack_into(">f", db, 0, pressure)
    db[4] = 1 if pressure > PLC_ALARM_BAR else 0


def serve_plc(line: Line, port: int, stop: threading.Event) -> None:
    import snap7
    from snap7.type import SrvArea

    db = bytearray(DB_SIZE)
    server = snap7.Server(log=False)
    server.register_area(SrvArea.DB, DB_NUMBER, db)
    server.start(tcp_port=port)
    log.info("PLC listening on port %d (DB%d)", port, DB_NUMBER)
    try:
        while not stop.wait(0.1):
            pack_db(db, line.transmitter())
    finally:
        server.stop()


# -- the physical world as seen by the robot -----------------------------------


def make_handler(line: Line, camera: Camera, plant: dict | None = None) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: dict) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _send_photo(self, file: str) -> None:
            try:
                data = camera.read(file)
            except KeyError:
                self._send(404, {"error": "unknown photo"})
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802 (http.server naming)
            if self.path.startswith("/camera/"):
                poi = self.path.removeprefix("/camera/")
                try:
                    photo = camera.shoot(poi, line)
                except KeyError:
                    self._send(404, {"error": "unknown point of interest"})
                    return
                self._send(200, {"poi": photo.poi, "photo": photo.file})
            elif self.path.startswith("/fotos/"):
                self._send_photo(self.path.removeprefix("/fotos/"))
            elif self.path == "/valves/xv-104":
                self._send(200, {"device": "XV-104", "closed": line.valve_closed})
            elif self.path == "/planta" and plant is not None:
                self._send(200, plant)
            elif self.path == "/escenario":
                sc = line.scenario
                self._send(
                    200,
                    {
                        "fase_s": round(line.phase(), 1),
                        "ciclo_s": sc.cycle_s,
                        "perturbacion_s": sc.profile.rise_at,
                        "valvula_cerrada": line.valve_closed,
                        "perfil": sc.profile.points(),
                    },
                )
            else:
                self._send(404, {"error": "unknown device"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path == "/escenario/perturbacion":
                if line.disturb_now():
                    self._send(200, {"ok": True})
                else:
                    self._send(409, {"ok": False, "error": "there is already a disturbance going on"})
            elif self.path == "/valves/xv-104/close":
                changed = line.close_valve()
                log.info("robot closed XV-104" if changed else "XV-104 was already closed")
                self._send(200, {"device": "XV-104", "closed": True, "changed": changed})
            else:
                self._send(404, {"error": "unknown action"})

        def log_message(self, *args) -> None:  # keep the logs about the plant, not HTTP
            pass

    return Handler


def main(argv: list[str] | None = None) -> int:
    from .bus import setup_logging, stop_on_signal
    from .plant import load_plant

    parser = argparse.ArgumentParser(
        prog="uns-world", description="Simulated plant: PLC, gauge, manual valve and extinguisher"
    )
    parser.add_argument("--plc-port", type=int, default=1102)
    parser.add_argument("--http-port", type=int, default=8080)
    args = parser.parse_args(argv)
    setup_logging()

    stop = stop_on_signal()
    dataset = default_dataset()
    line = Line(Scenario.from_env(dataset))
    camera = Camera(dataset)
    plant = load_plant().raw
    http = ThreadingHTTPServer(("0.0.0.0", args.http_port), make_handler(line, camera, plant))
    threading.Thread(target=http.serve_forever, daemon=True).start()
    threading.Thread(target=serve_plc, args=(line, args.plc_port, stop), daemon=True).start()
    log.info("world API on port %d, %d photos in the dataset", args.http_port, len(camera.photos))

    start = last = time.monotonic()
    while not stop.wait(0.1):
        now = time.monotonic()
        line.step(now - start, now - last)
        last = now
    http.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
