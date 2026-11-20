import struct
from pathlib import Path

import pytest

from uns_demo.world import NORMAL_BAR, Camera, Line, Profile, Scenario, pack_db

DATASET = Path(__file__).resolve().parents[1] / "dataset"


def scenario(scale=1.0):
    return Scenario(Profile.load(DATASET / "plc" / "linea-104.csv", scale))


def run(line, start, end, dt=0.1):
    t = start
    while t < end:
        t += dt
        line.step(t, dt)
    return t


def test_profile_interpolates_between_its_points():
    profile = Profile([(0, 4.0), (10, 6.0), (20, 6.0)])
    assert profile.at(5) == 5.0 and profile.at(15) == 6.0 and profile.at(99) == 6.0
    assert profile.cycle_s == 20


def test_the_shipped_profile_goes_from_normal_to_peak_and_back():
    profile = scenario().profile
    assert profile.cycle_s == 180
    assert profile.at(0) == profile.at(180) == NORMAL_BAR
    assert max(profile.at(t) for t in range(181)) == 6.8


def test_pressure_follows_the_profile_while_the_valve_is_open():
    line = Line(scenario(), seed=1)
    run(line, 0, 35)
    assert abs(line.pressure - NORMAL_BAR) < 0.01
    run(line, 35, 80)
    assert 6.0 < line.pressure <= 6.8


def test_closing_the_valve_brings_pressure_back_to_normal():
    line = Line(scenario(), seed=1)
    t = run(line, 0, 75)
    assert line.close_valve() is True
    assert line.close_valve() is False  # already closed
    run(line, t, t + 40)
    assert abs(line.pressure - NORMAL_BAR) < 0.05


def test_operator_reopens_the_valve_at_the_end_of_the_cycle():
    line = Line(scenario(), seed=1)
    line.close_valve()
    run(line, 0, line.scenario.reopen_at_s + 1)
    assert line.valve_closed is False


def test_time_scale_shortens_the_whole_story(monkeypatch):
    monkeypatch.setenv("WORLD_TIME_SCALE", "0.1")
    sc = Scenario.from_env(DATASET)
    assert sc.cycle_s == pytest.approx(18.0) and sc.reopen_at_s == pytest.approx(17.0)


def test_plc_data_block_layout():
    db = bytearray(8)
    pack_db(db, 6.7)
    assert abs(struct.unpack_from(">f", db, 0)[0] - 6.7) < 1e-6
    assert db[4] == 1  # high pressure bit (> 6.0 bar)
    pack_db(db, 4.2)
    assert db[4] == 0


def test_gauge_and_transmitter_see_the_same_line_with_their_own_error():
    line = Line(scenario(), seed=3)
    line.pressure = 6.8
    assert abs(line.transmitter() - 6.8) < 0.05
    assert abs(line.gauge() - 6.88) < 0.1


# -- the camera and the dataset ------------------------------------------------


def test_every_photo_listed_in_the_dataset_exists_and_has_a_recorded_answer():
    import hashlib
    import json

    camera = Camera(DATASET)
    answers = json.loads((DATASET / "respuestas-de-prueba.json").read_text(encoding="utf-8"))
    assert camera.pois() == ["EXT-12", "PI-104"]
    for photo in camera.photos:
        data = camera.read(photo.file)
        assert data[:2] == b"\xff\xd8", photo.file  # a JPEG
        assert hashlib.sha256(data).hexdigest() in answers, photo.file


def test_the_gauge_photo_matches_what_the_dial_shows():
    line, camera = Line(scenario(), seed=1), Camera(DATASET, seed=1, trouble_rate=0)
    for pressure, expected in [(4.12, "4.25"), (6.4, "6.50"), (6.9, "7.00")]:
        line.pressure = pressure  # the dial reads about 0.08 bar more
        photo = camera.shoot("pi-104", line)
        assert (photo.poi, photo.value, photo.condition) == ("PI-104", expected, "limpia")


def test_now_and_then_the_camera_gets_a_bad_photo():
    line, camera = Line(scenario(), seed=1), Camera(DATASET, seed=1, trouble_rate=1)
    line.pressure = 6.67  # dial ~6.75, where there is a blurred photo
    assert camera.shoot("pi-104", line).condition == "desenfoque"


def test_the_extinguisher_is_mostly_fine_and_its_state_lasts_several_rounds():
    camera = Camera(DATASET, seed=7, hold_rounds=8)
    line = Line(scenario(), seed=1)
    values = [camera.shoot("ext-12", line).value for _ in range(400)]
    assert 0.6 < values.count("OK") / len(values) < 0.95 and "NOK" in values
    changes = sum(1 for a, b in zip(values, values[1:], strict=False) if a != b)
    assert changes <= len(values) / 8  # never flips from one round to the next more than once per hold


def test_only_dataset_files_can_be_read():
    camera = Camera(DATASET)
    with pytest.raises(KeyError):
        camera.read("../fotos.csv")
    with pytest.raises(KeyError):
        camera.shoot("pt-104", Line(scenario()))


def test_a_disturbance_can_be_asked_for_and_starts_at_once():
    line = Line(scenario(), seed=1)
    run(line, 0, 5)
    assert line.disturb_now() is True
    t = run(line, 5, 30)
    assert line.pressure > 6.0
    assert line.disturb_now() is False  # one at a time
    line.close_valve()
    run(line, t, t + 40)
    assert line.disturb_now() is True and line.valve_closed is False  # reopened for the new one


def test_profile_knows_when_the_disturbance_starts():
    assert scenario().profile.rise_at == 40
