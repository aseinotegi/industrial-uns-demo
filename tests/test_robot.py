import pytest

from uns_demo import uns
from uns_demo.plant import load_plant
from uns_demo.robot import Robot
from uns_demo.uns import Message


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
    robot = Robot(world, load_plant(), publish=lambda t, m, r: sent.append((t, m, r)), **kwargs)
    return robot, world, sent


def run(robot, seconds, dt=0.2):
    for _ in range(int(seconds / dt)):
        robot.tick(dt)


def events(sent):
    """Everything but the position, which is published on every tick."""
    return [(t, m.value) for t, m, _ in sent if t != uns.ROBOT_POSE]


def test_a_round_visits_every_stop_in_order_and_comes_back_to_the_base():
    robot, _, sent = make(speed=2, dwell_s=1)
    length = robot._route.length
    run(robot, length / 2 + 3 * 1 + 1)  # one loop plus the time at the stops
    photos = [m.extra["poi"] for t, m, _ in sent if t == uns.ROBOT_PHOTO]
    assert photos == ["PI-104", "EXT-12"]
    assert (uns.XV_104_STATE, "abierta") in events(sent)
    (x, y), _ = robot._route.at(robot.s)
    assert abs(x - 6) < 2.5 and abs(y - 32) < 2.5  # back near the charging base


def test_photo_message_follows_the_inspector_contract():
    robot, _, sent = make(speed=5, dwell_s=0)
    run(robot, 10)
    photo = next(m for t, m, _ in sent if t == uns.ROBOT_PHOTO)
    assert photo.extra == {"poi": "PI-104", "image_url": "http://world:8080/fotos/pi-104.jpg"}
    assert photo.source == "robot-01"


def test_it_stops_at_each_point_for_the_dwell_time():
    robot, _, _ = make(speed=4, dwell_s=3)
    run(robot, 7.4)  # base → PI-104 is 28 m: 7 s at 4 m/s
    assert robot.state == "inspeccionando"
    s = robot.s
    run(robot, 2)
    assert robot.s == s  # still there
    run(robot, 1.6)
    assert robot.state == "en-ruta" and robot.s > s


def test_position_is_published_on_every_tick_with_coordinates():
    robot, _, sent = make()
    robot.tick(0.2)
    topic, pose, retain = sent[-1]
    assert topic == uns.ROBOT_POSE and retain is True
    assert pose.value == "en-ruta" and {"x", "y", "rumbo", "destino"} <= pose.extra.keys()
    assert pose.extra["destino"] == "PI-104"


def test_close_order_sends_it_to_the_valve_and_back_to_the_round():
    robot, world, sent = make(speed=4, dwell_s=1, action_s=2)
    run(robot, 2)
    robot.on_order(uns.XV_104_ORDER, Message("cerrar"))
    robot.tick(0.2)
    assert robot.state == "en-mision"
    run(robot, 20)
    assert world.closed
    values = [v for t, v in events(sent) if t in (uns.ROBOT_MISSION, uns.XV_104_STATE)]
    assert values[-3:] == ["en-camino", "cerrada", "completada"]
    assert robot.state in ("en-ruta", "inspeccionando", "comprobando")


def test_a_mission_takes_the_shorter_way():
    robot, _, sent = make(speed=4, dwell_s=0)
    robot.s = robot._plant.stop_for("EXT-12").s  # past the valve: going back is shorter
    robot.on_order(uns.XV_104_ORDER, Message("cerrar"))
    robot.tick(0.2)
    before = robot.s
    robot.tick(0.2)
    assert robot.s < before  # walking backwards along the route


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
    run(robot, 30)
    assert [v for t, v in events(sent) if t == uns.ROBOT_MISSION].count("en-camino") == 1


def test_photo_numbers_keep_increasing():
    robot, _, sent = make(speed=10, dwell_s=0)
    run(robot, 25)
    photos = [m.value for t, m, _ in sent if t == uns.ROBOT_PHOTO]
    assert photos[:3] == ["IMG_0411", "IMG_0412", "IMG_0413"]
