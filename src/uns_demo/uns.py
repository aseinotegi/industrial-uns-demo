"""The plant's Unified Namespace: where every value lives and what a message looks like.

Every service publishes its data under one hierarchy, so nobody needs to know who
produced a value to find it::

    planta1 / exterior / pt-104 / presion
    site      area       device   value

Topic names are the plant's own (Spanish, like the tags on the P&ID); code is English.

Message contract (JSON)::

    {"value": 6.8, "unit": "bar", "ts": "2026-11-18T10:00:00.000Z",
     "source": "vision", "quality": "good", "confidence": 0.98}

Only ``value`` is mandatory. Extra keys (``confidence``, ``image``...) are kept.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

SITE = "planta1"

_LEVEL_RE = re.compile(r"^[a-z0-9][a-z0-9\-]*$")


def path(*levels: str) -> str:
    """Builds a topic from its levels, refusing anything that is not lowercase-and-dashes."""
    for level in levels:
        if not _LEVEL_RE.match(level):
            raise ValueError(f"invalid UNS level '{level}' (lowercase letters, digits and '-')")
    return "/".join(levels)


# The part of the namespace this demo uses. One place, so a typo is a failing test,
# not a value that silently goes nowhere.
PI_104 = path(SITE, "exterior", "pi-104", "presion")  # local gauge, read by vision
PT_104 = path(SITE, "exterior", "pt-104", "presion")  # transmitter, read by the PLC
XV_104_ORDER = path(SITE, "exterior", "xv-104", "orden")  # what the rules ask for
XV_104_STATE = path(SITE, "exterior", "xv-104", "estado")  # what the robot saw / did
RULES = path(SITE, "exterior", "reglas")  # one subtopic per rule
ROBOT = path(SITE, "robots", "robot-01")
ROBOT_PHOTO = f"{ROBOT}/foto"
ROBOT_MISSION = f"{ROBOT}/mision"
SERVICES = path(SITE, "servicios")  # online/offline of every service (Last Will)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


@dataclass
class Message:
    value: Any
    unit: str | None = None
    ts: str = field(default_factory=now_iso)
    source: str | None = None
    quality: str = "good"
    extra: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        body: dict[str, Any] = {"value": self.value}
        if self.unit:
            body["unit"] = self.unit
        body["ts"] = self.ts
        if self.source:
            body["source"] = self.source
        body["quality"] = self.quality
        body.update(self.extra)
        return json.dumps(body, separators=(",", ":"), ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: bytes | str) -> Message:
        data = json.loads(raw)
        if not isinstance(data, dict) or "value" not in data:
            raise ValueError("a UNS message is a JSON object with at least 'value'")
        known = {k: data.pop(k) for k in ("value", "unit", "ts", "source", "quality") if k in data}
        known.setdefault("ts", now_iso())
        return cls(**known, extra=data)

    @property
    def number(self) -> float | None:
        """The value as a number when it is one (booleans count as 0/1)."""
        if isinstance(self.value, bool):
            return float(self.value)
        if isinstance(self.value, (int, float)):
            return float(self.value)
        return None
