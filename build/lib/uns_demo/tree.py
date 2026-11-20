"""Prints the Unified Namespace as a tree: the last value of everything, right now.

    docker compose run --rm tree

Every value in the UNS is published retained, so connecting and listening for a
moment is enough to see the whole plant.
"""

from __future__ import annotations

import argparse
import time

from . import uns
from .uns import Message


def render(values: dict[str, Message]) -> str:
    """Indented tree, one line per value: ``presion  6.8 bar  (inspector, 98 %)``."""
    lines: list[str] = []
    # values line up in one column, whatever their depth
    column = max((2 * t.count("/") + len(t.rsplit("/", 1)[-1]) for t in values), default=0) + 2
    shown: set[tuple[str, ...]] = set()
    for topic in sorted(values):
        levels = topic.split("/")
        for depth in range(len(levels) - 1):
            branch = tuple(levels[: depth + 1])
            if branch not in shown:
                shown.add(branch)
                lines.append(f"{'  ' * depth}{levels[depth]}/")
        message = values[topic]
        value = message.value
        if isinstance(value, float):
            value = f"{value:.2f}"
        text = f"{value} {message.unit}" if message.unit else str(value)
        notes = [n for n in (message.source, _confidence(message), _quality(message)) if n]
        suffix = f"   ({', '.join(notes)})" if notes else ""
        label = f"{'  ' * (len(levels) - 1)}{levels[-1]}"
        lines.append(f"{label:<{column}}{text}{suffix}")
    return "\n".join(lines)


def _confidence(message: Message) -> str | None:
    confidence = message.extra.get("confidence")
    return f"{confidence:.0%}" if isinstance(confidence, (int, float)) else None


def _quality(message: Message) -> str | None:
    return None if message.quality == "good" else message.quality


def main(argv: list[str] | None = None) -> int:
    import os

    import paho.mqtt.client as mqtt

    parser = argparse.ArgumentParser(prog="uns-tree", description="Print the UNS as a tree")
    parser.add_argument("--wait", type=float, default=2.0, help="seconds to collect retained values")
    args = parser.parse_args(argv)

    values: dict[str, Message] = {}

    def on_message(client, userdata, msg) -> None:
        try:
            values[msg.topic] = Message.from_json(msg.payload)
        except ValueError:
            pass

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"uns-tree-{os.getpid()}")
    client.on_message = on_message
    client.on_connect = lambda c, *_: c.subscribe(f"{uns.SITE}/#")
    client.connect(os.environ.get("MQTT_HOST", "localhost"), int(os.environ.get("MQTT_PORT", "1883")))
    client.loop_start()
    time.sleep(args.wait)
    client.loop_stop()
    print(render(values) or "(the namespace is empty: is the demo running?)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
