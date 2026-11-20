"""Historian: everything published under the site goes to the database.

* Numbers (pressures, booleans as 0/1) → ``measurements``, one row per value.
* Everything else (orders, valve states, alarms, missions) → ``events``, with the
  whole message as JSON.

Rows are written in batches (every second or every 500 rows), so a burst of values
costs one transaction, not hundreds. The schema is created on start. With
TimescaleDB both tables become hypertables; on plain PostgreSQL it still works.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime

from . import uns
from .uns import Message

log = logging.getLogger("historian")

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS measurements (
        time    timestamptz      NOT NULL,
        topic   text             NOT NULL,
        value   double precision NOT NULL,
        unit    text,
        source  text,
        quality text
    )""",
    """CREATE TABLE IF NOT EXISTS events (
        time    timestamptz NOT NULL,
        topic   text        NOT NULL,
        value   text,
        payload jsonb       NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS measurements_topic_time ON measurements (topic, time DESC)",
    "CREATE INDEX IF NOT EXISTS events_topic_time ON events (topic, time DESC)",
]

HYPERTABLES = [
    "SELECT create_hypertable('measurements', 'time', if_not_exists => TRUE, migrate_data => TRUE)",
    "SELECT create_hypertable('events', 'time', if_not_exists => TRUE, migrate_data => TRUE)",
]


# Live-only topics: the robot's position several times a second is for the console,
# not for the history (the photos and missions already say where it has been).
NOT_STORED = (uns.ROBOT_POSE, uns.SIM_SCENARIO)


def to_row(topic: str, message: Message) -> tuple[str, tuple]:
    """Which table a message goes to, and the row."""
    time = uns.parse_ts(message.ts)
    number = message.number
    # An inspection is a record (photo, checks, model), not a sample: it goes to events
    # even when it read a number, so the whole of it is kept.
    if number is not None and not topic.endswith("/inspeccion"):
        return "measurements", (time, topic, number, message.unit, message.source, message.quality)
    payload = json.loads(message.to_json())
    return "events", (time, topic, str(message.value), json.dumps(payload, ensure_ascii=False))


class Historian:
    def __init__(self, connect, flush_s: float = 1.0, max_rows: int = 500) -> None:
        self._connect = connect
        self._flush_s = flush_s
        self._max_rows = max_rows
        self._rows: dict[str, list[tuple]] = {"measurements": [], "events": []}
        self._lock = threading.Lock()
        self._conn = None

    def setup(self) -> None:
        conn = self._connection()
        with conn.cursor() as cur:
            for statement in SCHEMA:
                cur.execute(statement)
            cur.execute("SELECT 1 FROM pg_available_extensions WHERE name = 'timescaledb'")
            if cur.fetchone():
                cur.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
                for statement in HYPERTABLES:
                    cur.execute(statement)
                log.info("TimescaleDB hypertables ready")
            else:
                log.info("plain PostgreSQL (no TimescaleDB): regular tables")
        conn.commit()

    def add(self, topic: str, message: Message) -> None:
        if topic in NOT_STORED or topic.startswith(uns.SIMULATION):
            return
        table, row = to_row(topic, message)
        with self._lock:
            self._rows[table].append(row)
            full = sum(len(r) for r in self._rows.values()) >= self._max_rows
        if full:
            self.flush()

    def flush(self) -> int:
        with self._lock:
            batch, self._rows = self._rows, {"measurements": [], "events": []}
        count = sum(len(r) for r in batch.values())
        if not count:
            return 0
        try:
            conn = self._connection()
            with conn.cursor() as cur:
                if batch["measurements"]:
                    cur.executemany(
                        "INSERT INTO measurements (time, topic, value, unit, source, quality) "
                        "VALUES (%s, %s, %s, %s, %s, %s)",
                        batch["measurements"],
                    )
                if batch["events"]:
                    cur.executemany(
                        "INSERT INTO events (time, topic, value, payload) VALUES (%s, %s, %s, %s)",
                        batch["events"],
                    )
            conn.commit()
        except Exception as exc:  # noqa: BLE001 - any driver error: keep the rows, retry next flush
            log.warning("write failed (%s); %d rows kept for the next try", exc, count)
            self._conn = None
            with self._lock:
                for table, rows in batch.items():
                    self._rows[table] = rows + self._rows[table]
            return 0
        return count

    def run(self, stop: threading.Event) -> None:
        while not stop.wait(self._flush_s):
            self.flush()
        self.flush()

    def _connection(self):
        if self._conn is None or getattr(self._conn, "closed", False):
            self._conn = self._connect()
        return self._conn


def main() -> int:
    import psycopg

    from .bus import Bus, setup_logging, stop_on_signal

    setup_logging()
    dsn = os.environ.get("DATABASE_URL", "postgresql://uns:uns@localhost:5432/uns")
    historian = Historian(lambda: psycopg.connect(dsn))
    stop = stop_on_signal()
    while True:  # the database may still be starting
        try:
            historian.setup()
            break
        except psycopg.OperationalError as exc:
            log.info("waiting for the database: %s", str(exc).splitlines()[0])
            if stop.wait(3):
                return 0

    bus = Bus("historian")
    # Retained values were already stored when they were live.
    bus.subscribe(f"{uns.SITE}/#", historian.add, retained=False)
    bus.start()
    started = datetime.now().isoformat(timespec="seconds")
    log.info("storing %s/# since %s", uns.SITE, started)
    historian.run(stop)
    bus.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
