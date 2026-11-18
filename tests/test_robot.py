from uns_demo import uns
from uns_demo.robot import Robot
from uns_demo.uns import Message


class FakeWorld:
    def __init__(self):
        self.closed = False

    def valve_closed(self):
        return self.closed

    def close_valve(self):
        self.closed = True


def make():
    world, sent = FakeWorld(), []
    robot = Robot(
        world, publish=lambda t, m, r: sent.append((t, m.value, r)), travel_s=10, sleep=lambda s: None
    )
    return robot, world, sent


def test_round_takes_a_photo_and_reports_the_valve():
    robot, _, sent = make()
    robot.inspection_round()
    assert sent == [(uns.ROBOT_PHOTO, "IMG_0411", False), (uns.XV_104_STATE, "abierta", True)]


def test_close_order_runs_the_mission_and_reports_the_new_state():
    robot, world, sent = make()
    robot.on_order(uns.XV_104_ORDER, Message("cerrar"))
    assert world.closed
    assert [v for _, v, _ in sent] == ["en-camino", "cerrada", "completada"]


def test_unknown_orders_are_ignored():
    robot, world, sent = make()
    robot.on_order(uns.XV_104_ORDER, Message("abrir"))
    assert not world.closed and sent == []


def test_photo_numbers_keep_increasing():
    robot, _, sent = make()
    robot.inspection_round()
    robot.inspection_round()
    photos = [v for t, v, _ in sent if t == uns.ROBOT_PHOTO]
    assert photos == ["IMG_0411", "IMG_0412"]
