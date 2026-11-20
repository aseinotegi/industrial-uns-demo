"""Map of the plant (config/planta.yaml) and the geometry of the robot's route.

The route is a closed polyline. A position on it is a distance ``s`` from the first
point, between 0 and the length of the loop; the robot moves by changing ``s``.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path

import yaml

Point = tuple[float, float]


@dataclass(frozen=True)
class Stop:
    point: Point
    action: str  # "foto" | "valvula"
    device: str
    s: float  # where it is along the route


class Route:
    def __init__(self, points: list[Point]) -> None:
        if len(points) < 2:
            raise ValueError("a route needs at least two points")
        self.points = points
        self._starts: list[float] = []  # distance at the start of each segment
        total = 0.0
        for a, b in self._segments():
            self._starts.append(total)
            total += math.dist(a, b)
        self.length = total

    def _segments(self) -> list[tuple[Point, Point]]:
        closed = [*self.points, self.points[0]]
        return list(zip(closed, closed[1:], strict=False))

    def at(self, s: float) -> tuple[Point, float]:
        """Point and heading (degrees, 0 = towards +x, 90 = towards +y) at distance ``s``."""
        s %= self.length
        for (a, b), start in zip(self._segments(), self._starts, strict=True):
            seg = math.dist(a, b)
            if s <= start + seg or start == self._starts[-1]:
                f = 0.0 if seg == 0 else (s - start) / seg
                point = (a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f)
                heading = math.degrees(math.atan2(b[1] - a[1], b[0] - a[0]))
                return point, heading
        raise AssertionError("unreachable")

    def locate(self, point: Point) -> float:
        """Distance along the route of the route point closest to ``point``."""
        best_s, best_d = 0.0, math.inf
        for (a, b), start in zip(self._segments(), self._starts, strict=True):
            seg = math.dist(a, b)
            if seg == 0:
                continue
            t = ((point[0] - a[0]) * (b[0] - a[0]) + (point[1] - a[1]) * (b[1] - a[1])) / seg**2
            t = min(1.0, max(0.0, t))
            q = (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
            d = math.dist(point, q)
            if d < best_d:
                best_s, best_d = start + t * seg, d
        return best_s

    def ahead(self, s_from: float, s_to: float) -> float:
        """Distance going forward along the loop from ``s_from`` to ``s_to``."""
        return (s_to - s_from) % self.length


@dataclass
class Plant:
    raw: dict
    route: Route
    stops: list[Stop]

    def stop_for(self, device: str) -> Stop:
        for stop in self.stops:
            if stop.device == device:
                return stop
        raise KeyError(device)


def default_path() -> Path:
    if os.environ.get("PLANT_FILE"):
        return Path(os.environ["PLANT_FILE"])
    return Path(__file__).resolve().parents[2] / "config" / "planta.yaml"


def load_plant(path: Path | None = None) -> Plant:
    raw = yaml.safe_load((path or default_path()).read_text(encoding="utf-8"))
    route = Route([tuple(p) for p in raw["ruta"]])
    stops = []
    for item in raw.get("paradas", []):
        point = tuple(item["punto"])
        if item["accion"] not in ("foto", "valvula"):
            raise ValueError(f"unknown stop action {item['accion']!r}")
        stops.append(Stop(point, item["accion"], item["equipo"], route.locate(point)))
    stops.sort(key=lambda stop: stop.s)
    return Plant(raw, route, stops)
