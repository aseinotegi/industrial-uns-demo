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
