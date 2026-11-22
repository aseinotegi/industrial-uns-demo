import struct
from pathlib import Path

import pytest

from uns_demo.world import (
    LEVEL_START,
    LEVEL_STOP,
    NORMAL_BAR,
    Camera,
    Line,
    Process,
    Profile,
    Scenario,
    pack_db,
)

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
    assert profile.cycle_s == 360
    assert profile.at(0) == profile.at(360) == NORMAL_BAR
    assert max(profile.at(t) for t in range(361)) == 8.0


def test_pressure_follows_the_profile_while_the_valve_is_open():
    line = Line(scenario(), seed=1)
    run(line, 0, 75)
    assert abs(line.pressure - NORMAL_BAR) < 0.01
    run(line, 75, 150)
    assert 6.0 < line.pressure <= 8.0


def test_closing_the_valve_brings_pressure_back_to_normal():
    line = Line(scenario(), seed=1)
    t = run(line, 0, 150)
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
    assert sc.cycle_s == pytest.approx(36.0) and sc.reopen_at_s == pytest.approx(34.5)


def tags(**kw):
    base = {
        "pressure": 4.2,
        "flow": 36.0,
        "temperature": 38.0,
        "level": 50.0,
        "current": 0.0,
        "pump_on": False,
        "starts": 3,
    }
    return {**base, **kw}


def test_plc_data_block_layout():
    db = bytearray(24)
    pack_db(db, tags(pressure=6.7, pump_on=True, level=93.0, current=21.5))
    assert abs(struct.unpack_from(">f", db, 0)[0] - 6.7) < 1e-6
    assert db[4] == 0b111  # high pressure, pump running, high level
    flow, temp, level, current = struct.unpack_from(">ffff", db, 6)
    assert (round(flow, 1), round(temp, 1), round(level, 1), round(current, 1)) == (36.0, 38.0, 93.0, 21.5)
    assert struct.unpack_from(">h", db, 22)[0] == 3
    pack_db(db, tags())
    assert db[4] == 0


def test_the_plc_runs_the_pump_on_the_tank_level():
    line = Line(scenario(), seed=1)
    process = Process(line, seed=1)
    process.level = LEVEL_START - 1
    seen_on = seen_off_after = False
    for _ in range(6000):  # 10 minutes
        line.step(0, 0.1)
        process.step(0.1)
        if process.pump_on:
            seen_on = True
            assert process.current > 15
        elif seen_on:
            seen_off_after = True
            assert process.level <= LEVEL_STOP + 1
            break
    assert seen_on and seen_off_after and process.starts == 1


def test_closing_the_valve_stops_the_flow_into_the_tank():
    line = Line(scenario(), seed=1)
    process = Process(line, seed=1)
    for _ in range(200):
        process.step(0.1)
    assert process.flow > 30
    line.close_valve()
    for _ in range(200):
        process.step(0.1)
    assert process.flow < 1


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
    assert camera.pois() == ["EXT-12", "EXT-14", "PI-104", "PI-201", "PI-402", "PI-610", "PIV-801"]
    for photo in camera.photos:
        data = camera.read(photo.file)
        assert data[:2] == b"\xff\xd8", photo.file  # a JPEG
        assert hashlib.sha256(data).hexdigest() in answers, photo.file


def test_every_recorded_answer_fits_its_question_and_locates_the_object():
    """What the inspector checks first: a key the schema does not have rejects the whole answer."""
    import hashlib
    import json

    import yaml

    config = yaml.safe_load((DATASET.parent / "config" / "inspector.yaml").read_text(encoding="utf-8"))
    answers = json.loads((DATASET / "respuestas-de-prueba.json").read_text(encoding="utf-8"))
    common = {"value", "status_reason", "confidence", "evidence", "bbox_2d", "other_boxes"}
    camera = Camera(DATASET)
    for photo in camera.photos:
        answer = answers[hashlib.sha256(camera.read(photo.file)).hexdigest()]
        kind = config["profiles"][config["pois"][photo.poi]["profile"]]["kind"]
        assert set(answer) <= (common | {"unit"} if kind == "numeric" else common), photo.file
        assert answer["bbox_2d"], photo.file
        expected = None if photo.value == "" else photo.value if kind == "state" else float(photo.value)
        assert answer["value"] == expected, photo.file  # the round shows the true reading


def test_the_gauge_photo_is_on_the_same_side_of_the_alarm_as_the_dial():
    line, camera = Line(scenario(), seed=1), Camera(DATASET, seed=1)
    for pressure, expected in [(4.12, "5.3"), (5.7, "5.3"), (6.1, "8.0"), (6.9, "8.0")]:
        line.pressure = pressure  # the dial reads about 0.08 bar more
        photo = camera.shoot("pi-104", line)
        assert (photo.poi, photo.value) == ("PI-104", expected), pressure


def test_the_extinguisher_is_mostly_fine_and_its_state_lasts_several_rounds():
    camera = Camera(DATASET, seed=7, hold_rounds=8)
    line = Line(scenario(), seed=1)
    values = [camera.shoot("ext-12", line).value for _ in range(400)]
    assert 0.6 < values.count("OK") / len(values) < 0.95 and "NOK" in values
    changes = sum(1 for a, b in zip(values, values[1:], strict=False) if a != b)
    assert changes <= len(values) / 8  # never flips from one round to the next more than once per hold


def test_real_photos_carry_their_credit():
    real = Camera(DATASET).credits()  # every photo is a real one
    assert real and all(
        c["licencia"].startswith("CC") and "wikimedia.org" in c["fuente"] for c in real.values()
    )


def test_a_gauge_that_does_not_move_always_gives_its_photo():
    camera = Camera(DATASET, seed=1)
    assert {camera.shoot("pi-610", Line(scenario())).file for _ in range(5)} == {"pi-610_21.0.jpg"}


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
    t = run(line, 5, 50)
    assert line.pressure > 6.0
    assert line.disturb_now() is False  # one at a time
    line.close_valve()
    run(line, t, t + 40)
    assert line.disturb_now() is True and line.valve_closed is False  # reopened for the new one


def test_profile_knows_when_the_disturbance_starts():
    assert scenario().profile.rise_at == 80
