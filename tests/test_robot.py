import pytest

from uns_demo import uns
from uns_demo.plant import load_plant
from uns_demo.robot import Robot
from uns_demo.uns import Message

ROUND = ["PIV-801", "EXT-14", "PI-104", "PI-201", "PI-402", "PI-610", "EXT-12"]  # photo stops in order


class FakeWorld:
    def __init__(self):
        self.closed = False

    def photo(self, poi):
        return f"http://world:8080/fotos/{poi.lower()}.jpg"

    def valve_closed(self):
        return self.closed

    def close_valve(self):
        self.closed = True


def make(**kwargs):
    world, sent = FakeWorld(), []
    kwargs = {"accel": 100.0, **kwargs}  # instant speed changes unless a test is about them
    robot = Robot(world, load_plant(), publish=lambda t, m, r: sent.append((t, m, r)), **kwargs)
    return robot, world, sent


def run(robot, seconds, dt=0.2):
    for _ in range(int(seconds / dt)):
        robot.tick(dt)


def until(robot, done, limit_s=600, dt=0.2):
    for _ in range(int(limit_s / dt)):
        robot.tick(dt)
        if done():
            return
    raise AssertionError("did not happen in time")


def events(sent):
    """Everything but the position, which is published on every tick."""
    return [(t, m.value) for t, m, _ in sent if t != uns.ROBOT_POSE]


def photos(sent):
    return [m.extra["poi"] for t, m, _ in sent if t == uns.ROBOT_PHOTO]


def test_a_round_visits_every_stop_in_order_and_comes_back_to_the_base():
    robot, _, sent = make(speed=5, dwell_s=0.4)
    until(robot, lambda: len(photos(sent)) == len(ROUND))
    assert photos(sent) == ROUND
    assert (uns.XV_104_STATE, "abierta") in events(sent)
    until(robot, lambda: robot.s < 1e-6 or robot._route.length - robot.s < 1e-6)  # back at the base


def test_photo_message_follows_the_inspector_contract():
    robot, _, sent = make(speed=5, dwell_s=0)
    until(robot, lambda: photos(sent))
    photo = next(m for t, m, _ in sent if t == uns.ROBOT_PHOTO)
    assert photo.extra == {"poi": "PIV-801", "image_url": "http://world:8080/fotos/piv-801.jpg"}
    assert photo.source == "robot-01"


def test_it_stops_at_each_point_for_the_dwell_time():
    robot, _, sent = make(speed=4, dwell_s=3)
    until(robot, lambda: robot.state == "inspeccionando")
    s = robot.s
    run(robot, 2)
    assert robot.s == s and robot.state == "inspeccionando"
    run(robot, 1.6)
    assert robot.state == "en-ruta" and robot.s > s


def test_it_speeds_up_and_slows_down_instead_of_jumping():
    robot, _, _ = make(speed=1.5, accel=0.5, dwell_s=1)
    robot.tick(0.2)
    assert robot.v == pytest.approx(0.1)  # 0.5 m/s² for 0.2 s
    speeds = []
    until(robot, lambda: speeds.append(robot.v) or robot.state == "inspeccionando")
    assert max(speeds) == pytest.approx(1.5)
    assert speeds[-2] < 0.6  # it braked before the stop


def test_position_is_published_on_every_tick_with_coordinates_and_battery():
    robot, _, sent = make()
    robot.tick(0.2)
    topic, pose, retain = sent[-1]
    assert topic == uns.ROBOT_POSE and retain is True
    assert pose.value == "en-ruta"
    assert {"x", "y", "rumbo", "velocidad", "bateria", "destino"} <= pose.extra.keys()
    assert pose.extra["destino"] == "PIV-801"


def test_walking_spends_battery_and_the_base_charges_it():
    robot, _, sent = make(speed=10, dwell_s=0, charge_rate=20, charge_below=60)
    start = robot.battery
    until(robot, lambda: len(photos(sent)) == 1)
    assert robot.battery < start
    robot.battery = 40  # low: at the base it must stop and charge
    until(robot, lambda: robot.state == "cargando")
    until(robot, lambda: robot.state == "en-ruta", limit_s=30)
    assert robot.battery >= 95


def test_with_enough_battery_it_does_not_stop_at_the_base():
    robot, _, sent = make(speed=10, dwell_s=0, charge_below=10)
    until(robot, lambda: len(photos(sent)) == len(ROUND) + 1)
    assert "cargando" not in [m.value for t, m, _ in sent if t == uns.ROBOT_POSE]


def test_close_order_sends_it_to_the_valve_and_back_to_the_round():
    robot, world, sent = make(speed=4, dwell_s=1, action_s=2)
    run(robot, 2)
    robot.on_order(uns.XV_104_ORDER, Message("cerrar"))
    robot.tick(0.2)
    assert robot.state == "en-mision"
    until(robot, lambda: world.closed and robot.state != "actuando")
    values = [v for t, v in events(sent) if t in (uns.ROBOT_MISSION, uns.XV_104_STATE)]
    assert values[-3:] == ["en-camino", "cerrada", "completada"]


def test_a_mission_takes_the_shorter_way():
    robot, _, _ = make(speed=4, dwell_s=0)
    robot.s = robot._plant.stop_for("PI-201").s  # past the valve: going back is shorter
    robot.on_order(uns.XV_104_ORDER, Message("cerrar"))
    robot.tick(0.2)
    before = robot.s
    robot.tick(0.2)
    assert robot.s < before  # walking backwards along the route


def test_an_order_takes_it_out_of_a_charge():
    robot, world, _ = make(speed=4, dwell_s=0, action_s=0.2)
    robot.state, robot.battery = "cargando", 50
    robot.on_order(uns.XV_104_ORDER, Message("cerrar"))
    robot.tick(0.2)
    assert robot.state == "en-mision"
    until(robot, lambda: world.closed)


@pytest.mark.parametrize("value", ["abrir", "", 1])
def test_unknown_orders_are_ignored(value):
    robot, world, _ = make()
    robot.on_order(uns.XV_104_ORDER, Message(value))
    robot.tick(0.2)
    assert robot.state == "en-ruta" and not world.closed


def test_repeated_orders_during_a_mission_are_one_mission():
    robot, _, sent = make(speed=4, dwell_s=0, action_s=0.5)
    for _ in range(3):
        robot.on_order(uns.XV_104_ORDER, Message("cerrar"))
    run(robot, 60)
    assert [v for t, v in events(sent) if t == uns.ROBOT_MISSION].count("en-camino") == 1


def test_photo_numbers_keep_increasing():
    robot, _, sent = make(speed=10, dwell_s=0)
    until(robot, lambda: len(photos(sent)) >= 3)
    numbers = [m.value for t, m, _ in sent if t == uns.ROBOT_PHOTO]
    assert numbers[:3] == ["IMG_0411", "IMG_0412", "IMG_0413"]
