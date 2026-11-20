import math

import pytest

from uns_demo.plant import Route, load_plant


def test_route_length_and_points_along_it():
    route = Route([(0, 0), (10, 0), (10, 5)])
    assert route.length == pytest.approx(10 + 5 + math.hypot(10, 5))
    assert route.at(5) == ((5, 0), 0)
    (x, y), heading = route.at(12)
    assert (x, y) == (10, 2) and heading == 90
    assert route.at(route.length + 5)[0] == (5, 0)  # it loops


def test_locate_finds_the_closest_point_of_the_route():
    route = Route([(0, 0), (10, 0), (10, 5)])
    assert route.locate((4, -1)) == pytest.approx(4)
    assert route.ahead(8, 2) == pytest.approx(route.length - 6)


def test_the_shipped_plant_has_its_stops_on_the_route_in_order():
    plant = load_plant()
    assert [s.device for s in plant.stops] == ["PI-104", "XV-104", "EXT-12"]
    for stop in plant.stops:
        assert plant.route.at(stop.s)[0] == pytest.approx(stop.point)
    devices = {d["id"] for d in plant.raw["equipos"]}
    assert {s.device for s in plant.stops} <= devices


def test_unknown_stop_actions_are_refused(tmp_path):
    path = tmp_path / "planta.yaml"
    path.write_text("ruta: [[0, 0], [5, 0]]\nparadas: [{punto: [1, 0], accion: bailar, equipo: X}]")
    with pytest.raises(ValueError, match="bailar"):
        load_plant(path)
