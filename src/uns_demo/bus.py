"""Thin MQTT layer shared by every service: connect, publish UNS messages, subscribe.

Each service announces itself under ``planta1/servicios/<name>`` (retained), and the
broker flips it to ``offline`` through the Last Will if the service dies.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable

import paho.mqtt.client as mqtt

from . import uns
from .uns import Message

log = logging.getLogger(__name__)

Handler = Callable[[str, Message], None]


class Bus:
    def __init__(self, name: str, host: str | None = None, port: int | None = None) -> None:
        self.name = name
        self._host = host or os.environ.get("MQTT_HOST", "localhost")
        self._port = port or int(os.environ.get("MQTT_PORT", "1883"))
        self._handlers: list[tuple[str, Handler, bool]] = []
        self._connected = threading.Event()
        self._foreign: set[str] = set()  # topics with non-UNS payloads, reported once
        self._status_topic = f"{uns.SERVICES}/{name}"
        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"uns-{name}")
        self._client.will_set(
            self._status_topic, Message("offline", source=name).to_json(), qos=1, retain=True
        )
        if os.environ.get("MQTT_USERNAME"):  # the broker has one user per service, with its ACL
            self._client.username_pw_set(os.environ["MQTT_USERNAME"], os.environ.get("MQTT_PASSWORD"))
        self._client.reconnect_delay_set(min_delay=1, max_delay=15)
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message

    # -- lifecycle --------------------------------------------------------------

    def start(self, wait_s: float = 30.0) -> None:
        self._client.connect_async(self._host, self._port, keepalive=30)
        self._client.loop_start()
        if not self._connected.wait(wait_s):
            log.warning("broker %s:%d not reachable yet; still trying", self._host, self._port)

    def stop(self) -> None:
        info = self._client.publish(
            self._status_topic, Message("offline", source=self.name).to_json(), 1, True
        )
        try:
            info.wait_for_publish(timeout=2)
        except (RuntimeError, ValueError):
            pass
        self._client.loop_stop()
        self._client.disconnect()

    # -- messaging --------------------------------------------------------------

    def publish(self, topic: str, message: Message, retain: bool = True) -> None:
        if message.source is None:
            message.source = self.name
        self._client.publish(topic, message.to_json(), qos=1, retain=retain)

    def clear(self, topic: str) -> None:
        """Removes a retained value from the broker (an empty retained message)."""
        self._client.publish(topic, b"", qos=1, retain=True)

    def subscribe(self, pattern: str, handler: Handler, retained: bool = True) -> None:
        """``retained=False`` skips the broker's stored last values: history, not news."""
        self._handlers.append((pattern, handler, retained))
        if self._connected.is_set():
            self._client.subscribe(pattern, qos=1)

    # -- paho callbacks ---------------------------------------------------------

    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        if reason_code.is_failure:
            log.error("broker refused the connection: %s", reason_code)
            return
        log.info("connected to %s:%d", self._host, self._port)
        client.publish(self._status_topic, Message("online", source=self.name).to_json(), qos=1, retain=True)
        for pattern, _, _ in self._handlers:  # also after a reconnection
            client.subscribe(pattern, qos=1)
        self._connected.set()

    def _on_message(self, client, userdata, msg) -> None:
        try:
            message = Message.from_json(msg.payload)
        except ValueError as exc:
            # Other tools may publish their own diagnostics under the site (e.g. the
            # gateway's stats). Not an error: say it once and move on.
            if msg.topic not in self._foreign:
                self._foreign.add(msg.topic)
                log.info("skipping %s: not a UNS message (%s)", msg.topic, exc)
            return
        for pattern, handler, retained in self._handlers:
            if msg.retain and not retained:
                continue
            if mqtt.topic_matches_sub(pattern, msg.topic):
                try:
                    handler(msg.topic, message)
                except Exception:  # a bad message must not kill the service
                    log.exception("handler for %s failed on %s", pattern, msg.topic)


def stop_on_signal() -> threading.Event:
    """An event that is set on SIGINT/SIGTERM, so services can shut down cleanly."""
    import signal

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    return stop


def setup_logging() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
