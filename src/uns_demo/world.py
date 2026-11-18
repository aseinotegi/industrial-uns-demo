"""The physical plant, simulated: what in real life is steel, not software.

It owns the truth that the other services can only observe:

* the line pressure, measured twice: by transmitter PT-104 (wired to the PLC) and
  by local gauge PI-104 (a dial with no signal; only a photo can read it);
* manual valve XV-104, which only the robot can close.

It exposes them the way a real plant would:

* a Siemens S7 PLC (DB1) for the gateway to read, on port 1102;
* a tiny HTTP API standing in for the physical world: the robot's camera "looks"
  at the gauge (``GET /gauges/pi-104``) and its arm turns the valve
  (``POST /valves/xv-104``).

Scenario, repeated every ``cycle_s`` seconds: an upstream disturbance pushes the
pressure from 4.2 towards 6.8 bar. Closing XV-104 brings it back down. Near the end
of the cycle an operator reopens the valve, ready for the next round.
"""

from __future__ import annotations

import argparse
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

log = logging.getLogger("world")

NORMAL_BAR = 4.2
PEAK_BAR = 6.8
PLC_ALARM_BAR = 6.0  # the PLC's own high-pressure bit


@dataclass
class Scenario:
    cycle_s: float = 180.0
    disturbance_at_s: float = 40.0  # when the upstream disturbance starts
    rise_s: float = 20.0  # time to go from normal to peak
    disturbance_ends_s: float = 150.0  # it fades on its own if nobody acts
    reopen_at_s: float = 170.0  # operator reopens XV-104
    decay_s: float = 6.0  # time constant once the valve is closed

    @classmethod
    def from_env(cls) -> Scenario:
        scale = float(os.environ.get("WORLD_TIME_SCALE", "1"))  # < 1 runs the story faster
        base = cls()
        return cls(**{k: v * scale for k, v in base.__dict__.items()})


class Line:
    """Pressure of the line around PT-104 / PI-104 and position of XV-104."""

    def __init__(self, scenario: Scenario | None = None, seed: int | None = None) -> None:
        self.scenario = scenario or Scenario()
        self.pressure = NORMAL_BAR
        self.valve_closed = False
        self._rng = random.Random(seed)
        self._lock = threading.Lock()

    def step(self, t: float, dt: float) -> None:
        """Advances the physics to time ``t`` (seconds since start)."""
        sc = self.scenario
        phase = t % sc.cycle_s
        with self._lock:
            if self.valve_closed and phase >= sc.reopen_at_s:
                self.valve_closed = False
                log.info("operator reopened XV-104")
            disturbed = sc.disturbance_at_s <= phase < sc.disturbance_ends_s
            if self.valve_closed or not disturbed:
                target, tau = NORMAL_BAR, sc.decay_s
            else:
                target, tau = PEAK_BAR, sc.rise_s / 3
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


def make_handler(line: Line) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: dict) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802 (http.server naming)
            if self.path == "/gauges/pi-104":
                self._send(200, {"device": "PI-104", "pressure": line.gauge(), "unit": "bar"})
            elif self.path == "/valves/xv-104":
                self._send(200, {"device": "XV-104", "closed": line.valve_closed})
            else:
                self._send(404, {"error": "unknown device"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path == "/valves/xv-104/close":
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

    parser = argparse.ArgumentParser(
        prog="uns-world", description="Simulated plant: PLC + gauge + manual valve"
    )
    parser.add_argument("--plc-port", type=int, default=1102)
    parser.add_argument("--http-port", type=int, default=8080)
    args = parser.parse_args(argv)
    setup_logging()

    stop = stop_on_signal()
    line = Line(Scenario.from_env())
    http = ThreadingHTTPServer(("0.0.0.0", args.http_port), make_handler(line))
    threading.Thread(target=http.serve_forever, daemon=True).start()
    threading.Thread(target=serve_plc, args=(line, args.plc_port, stop), daemon=True).start()
    log.info("world API on port %d", args.http_port)

    start = last = time.monotonic()
    while not stop.wait(0.1):
        now = time.monotonic()
        line.step(now - start, now - last)
        last = now
    http.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
