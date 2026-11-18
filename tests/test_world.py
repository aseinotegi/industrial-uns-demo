import struct

from uns_demo.world import NORMAL_BAR, PEAK_BAR, Line, Scenario, pack_db


def run(line, start, end, dt=0.1):
    t = start
    while t < end:
        t += dt
        line.step(t, dt)
    return t


def test_pressure_stays_normal_before_the_disturbance():
    line = Line(seed=1)
    run(line, 0, 35)
    assert abs(line.pressure - NORMAL_BAR) < 0.01


def test_disturbance_pushes_pressure_above_the_alarm_and_towards_the_peak():
    line = Line(seed=1)
    run(line, 0, 70)
    assert 6.0 < line.pressure <= PEAK_BAR


def test_closing_the_valve_brings_pressure_back_to_normal():
    line = Line(seed=1)
    t = run(line, 0, 70)
    assert line.close_valve() is True
    assert line.close_valve() is False  # already closed
    run(line, t, t + 40)
    assert abs(line.pressure - NORMAL_BAR) < 0.05


def test_operator_reopens_the_valve_at_the_end_of_the_cycle():
    line = Line(seed=1)
    line.close_valve()
    run(line, 0, Scenario().reopen_at_s + 1)
    assert line.valve_closed is False


def test_time_scale_shortens_the_whole_story(monkeypatch):
    monkeypatch.setenv("WORLD_TIME_SCALE", "0.1")
    scenario = Scenario.from_env()
    assert scenario.cycle_s == 18.0 and scenario.disturbance_at_s == 4.0


def test_plc_data_block_layout():
    db = bytearray(8)
    pack_db(db, 6.7)
    assert abs(struct.unpack_from(">f", db, 0)[0] - 6.7) < 1e-6
    assert db[4] == 1  # high pressure bit (> 6.0 bar)
    pack_db(db, 4.2)
    assert db[4] == 0


def test_gauge_and_transmitter_see_the_same_line_with_their_own_error():
    line = Line(seed=3)
    line.pressure = 6.8
    assert abs(line.transmitter() - 6.8) < 0.05
    assert abs(line.gauge() - 6.88) < 0.1
