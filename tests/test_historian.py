import json
from datetime import timezone

from uns_demo import uns
from uns_demo.historian import Historian, to_row
from uns_demo.uns import Message


def test_numbers_go_to_measurements():
    table, row = to_row(uns.PT_104, Message(6.71, unit="bar", ts="2026-11-18T10:00:00.000Z"))
    assert table == "measurements"
    time, topic, value, unit, source, quality = row
    assert time.tzinfo == timezone.utc and time.hour == 10
    assert (topic, value, unit, source, quality) == (uns.PT_104, 6.71, "bar", None, "good")


def test_booleans_are_stored_as_0_1():
    table, row = to_row("planta1/exterior/pt-104/alarma-alta", Message(True))
    assert table == "measurements" and row[2] == 1.0


def test_text_goes_to_events_with_the_whole_message():
    message = Message("activa", source="rules", extra={"inputs": {uns.PI_104: 6.8}})
    table, (_, topic, value, payload) = to_row(f"{uns.RULES}/r", message)
    assert table == "events" and value == "activa"
    assert json.loads(payload)["inputs"] == {uns.PI_104: 6.8}


class FakeCursor:
    def __init__(self, db):
        self.db = db

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def executemany(self, sql, rows):
        if self.db.fail:
            raise OSError("database restarting")
        table = "measurements" if "measurements" in sql else "events"
        self.db.rows[table] += rows


class FakeConnection:
    closed = False

    def __init__(self):
        self.rows = {"measurements": [], "events": []}
        self.fail = False

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        pass


def test_flush_writes_one_batch_per_table():
    db = FakeConnection()
    historian = Historian(lambda: db)
    historian.add(uns.PT_104, Message(6.7))
    historian.add(uns.PI_104, Message(6.8))
    historian.add(uns.XV_104_STATE, Message("cerrada"))
    assert historian.flush() == 3
    assert len(db.rows["measurements"]) == 2 and len(db.rows["events"]) == 1
    assert historian.flush() == 0


def test_rows_survive_a_database_outage():
    db = FakeConnection()
    historian = Historian(lambda: db)
    historian.add(uns.PT_104, Message(6.7))
    db.fail = True
    assert historian.flush() == 0
    historian.add(uns.PT_104, Message(6.6))
    db.fail = False
    assert historian.flush() == 2
    assert [r[2] for r in db.rows["measurements"]] == [6.7, 6.6]  # order kept


def test_large_bursts_flush_on_their_own():
    db = FakeConnection()
    historian = Historian(lambda: db, max_rows=10)
    for i in range(25):
        historian.add(uns.PT_104, Message(4.0 + i / 100))
    assert len(db.rows["measurements"]) == 20


def test_an_inspection_is_kept_whole_even_when_it_read_a_number():
    table, row = to_row(
        "planta1/auxiliares/pi-610/presion/inspeccion", Message(21.0, extra={"poi": "PI-610"})
    )
    assert table == "events" and '"poi": "PI-610"' in row[3]


def test_the_robot_position_and_the_scenario_are_not_stored():
    historian = Historian(connect=lambda: None)
    historian.add(uns.ROBOT_POSE, Message("en-ruta"))
    historian.add(uns.SIM_SCENARIO, Message(12.0))
    assert historian._rows == {"measurements": [], "events": []}


class Recorder:
    """A cursor that records what it is asked to run."""

    def __init__(self, existing=False):
        self.sql = []
        self.existing = existing

    def execute(self, statement, params=None):
        self.sql.append(repr(statement))

    def fetchone(self):
        return (1,) if self.existing else None


def test_grafana_gets_a_user_that_can_only_read():
    cur = Recorder()
    Historian._reader(cur, "s3cret")
    text = " ".join(cur.sql)
    assert "CREATE ROLE" in text and "s3cret" in text
    assert "GRANT SELECT ON measurements, events" in text
    assert "INSERT" not in text and "DELETE" not in text and "UPDATE" not in text


def test_an_existing_reader_gets_its_password_updated_not_recreated():
    cur = Recorder(existing=True)
    Historian._reader(cur, "new")
    assert "ALTER ROLE" in " ".join(cur.sql)
