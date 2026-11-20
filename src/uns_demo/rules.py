"""Rule engine: cross-checks values from the UNS and decides.

Rules live in a YAML file, readable by whoever runs the plant::

    - id: sobrepresion-linea-104
      mode: act                 # notify: raise the alarm only · act: also send the order
      when:                     # all of them, each one recent enough
        - { topic: planta1/exterior/pi-104/presion, above: 6.0, max_age_s: 45, quality: good }
        - { topic: planta1/exterior/pt-104/presion, above: 6.0, max_age_s: 30 }
      clear_when:               # any of them (hysteresis: below 5.5, not 6.0)
        - { topic: planta1/exterior/pt-104/presion, below: 5.5 }
      on_trigger:
        - { topic: planta1/exterior/xv-104/orden, value: cerrar }

Design choices:

* Two independent sources must agree. One bad photo or one noisy transmitter is not
  enough to send a robot.
* A value too old does not count: ``max_age_s`` is measured with the engine's clock
  when the value arrived, so a device with a wrong clock cannot fake freshness.
* Once active, a rule does not fire again until it clears. No order storms.
* ``notify`` first, ``act`` once the rule has earned trust. Same file, one word.
* The engine is pure (messages in, messages out), so it is tested without a broker.
"""

from __future__ import annotations

import logging
import operator
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import uns
from .uns import Message

log = logging.getLogger("rules")


class RuleError(ValueError):
    pass


COMPARISONS = ("above", "below", "equals", "not_equals")


@dataclass(frozen=True)
class Condition:
    topic: str
    threshold: object  # a number for above/below, any value for equals/not_equals
    compare: str  # one of COMPARISONS
    max_age_s: float | None = None
    quality: str | None = None

    def holds(self, value: Message | None, received_at: float | None, now: float) -> bool:
        if value is None or received_at is None:
            return False
        if self.max_age_s is not None and now - received_at > self.max_age_s:
            return False
        if self.quality is not None and value.quality != self.quality:
            return False
        if self.compare == "equals":
            return value.value == self.threshold
        if self.compare == "not_equals":
            return value.value != self.threshold
        if value.number is None:
            return False
        compare = operator.gt if self.compare == "above" else operator.lt
        return compare(value.number, self.threshold)

    @classmethod
    def parse(cls, raw: dict, where: str) -> Condition:
        if not isinstance(raw, dict) or "topic" not in raw:
            raise RuleError(f"{where}: each condition needs a 'topic'")
        limits = [k for k in COMPARISONS if k in raw]
        if len(limits) != 1:
            raise RuleError(f"{where}: use exactly one of {', '.join(COMPARISONS)}")
        unknown = set(raw) - {"topic", "max_age_s", "quality", *COMPARISONS}
        if unknown:
            raise RuleError(f"{where}: unknown keys {sorted(unknown)}")
        compare = limits[0]
        threshold = raw[compare]
        if compare in ("above", "below"):
            threshold = float(threshold)
        return cls(
            topic=str(raw["topic"]),
            threshold=threshold,
            compare=compare,
            max_age_s=float(raw["max_age_s"]) if "max_age_s" in raw else None,
            quality=raw.get("quality"),
        )


@dataclass(frozen=True)
class Action:
    topic: str
    value: object


@dataclass
class Rule:
    id: str
    description: str
    mode: str
    when: list[Condition]
    clear_when: list[Condition]
    on_trigger: list[Action] = field(default_factory=list)
    active: bool = False

    @property
    def topics(self) -> set[str]:
        return {c.topic for c in self.when + self.clear_when}


def parse_rules(raw: object) -> list[Rule]:
    if not isinstance(raw, dict) or not isinstance(raw.get("rules"), list) or not raw["rules"]:
        raise RuleError("the file needs a non-empty 'rules' list")
    rules: list[Rule] = []
    for i, item in enumerate(raw["rules"]):
        where = f"rule #{i + 1}"
        if not isinstance(item, dict) or "id" not in item:
            raise RuleError(f"{where}: missing 'id'")
        rule_id = str(item["id"])
        where = f"rule '{rule_id}'"
        try:
            uns.path(rule_id)
        except ValueError:
            raise RuleError(f"{where}: id must be lowercase letters, digits and '-'") from None
        mode = item.get("mode", "notify")
        if mode not in ("notify", "act"):
            raise RuleError(f"{where}: mode must be 'notify' or 'act'")
        when = [Condition.parse(c, where) for c in item.get("when") or []]
        clear_when = [Condition.parse(c, where) for c in item.get("clear_when") or []]
        if not when or not clear_when:
            raise RuleError(f"{where}: needs 'when' and 'clear_when'")
        actions = []
        for action in item.get("on_trigger") or []:
            if not isinstance(action, dict) or {"topic", "value"} - action.keys():
                raise RuleError(f"{where}: each action needs 'topic' and 'value'")
            actions.append(Action(str(action["topic"]), action["value"]))
        rules.append(Rule(rule_id, str(item.get("description", "")), mode, when, clear_when, actions))
    ids = [r.id for r in rules]
    if len(ids) != len(set(ids)):
        raise RuleError("rule ids must be unique")
    return rules


def load_rules(path: str | Path) -> list[Rule]:
    return parse_rules(yaml.safe_load(Path(path).read_text(encoding="utf-8")))


Output = tuple[str, Message, bool]  # topic, message, retain


class RuleEngine:
    def __init__(self, rules: list[Rule], clock: Callable[[], float] = time.monotonic) -> None:
        self.rules = rules
        self._clock = clock
        self._last: dict[str, tuple[Message, float]] = {}

    @property
    def topics(self) -> set[str]:
        return set().union(*(r.topics for r in self.rules))

    def replace(self, rules: list[Rule]) -> None:
        """New rules from an edited file. A rule that was active stays active (same id),
        so saving the file never sends the same order twice."""
        active = {r.id for r in self.rules if r.active}
        for rule in rules:
            rule.active = rule.id in active
        self.rules = rules

    def on_message(self, topic: str, message: Message) -> list[Output]:
        now = self._clock()
        self._last[topic] = (message, now)
        outputs: list[Output] = []
        for rule in self.rules:
            if topic in rule.topics:
                outputs += self._evaluate(rule, now)
        return outputs

    def _holds(self, condition: Condition, now: float) -> bool:
        message, received_at = self._last.get(condition.topic, (None, None))
        return condition.holds(message, received_at, now)

    def _evaluate(self, rule: Rule, now: float) -> list[Output]:
        if not rule.active and all(self._holds(c, now) for c in rule.when):
            rule.active = True
            log.warning("rule %s ACTIVE (%s)", rule.id, rule.mode)
            outputs = [self._state(rule, "activa")]
            if rule.mode == "act":
                outputs += [
                    (a.topic, Message(a.value, extra={"rule": rule.id}), False) for a in rule.on_trigger
                ]
            return outputs
        if rule.active and any(self._holds(c, now) for c in rule.clear_when):
            rule.active = False
            log.info("rule %s cleared", rule.id)
            return [self._state(rule, "normal")]
        return []

    def _state(self, rule: Rule, state: str) -> Output:
        inputs = {t: self._last[t][0].value for t in sorted(rule.topics) if t in self._last}
        extra = {"rule": rule.id, "description": rule.description, "mode": rule.mode, "inputs": inputs}
        return f"{uns.RULES}/{rule.id}", Message(state, extra=extra), True


def main() -> int:
    import os

    from .bus import Bus, setup_logging, stop_on_signal

    setup_logging()
    path = Path(os.environ.get("RULES_FILE", "config/rules.yaml"))
    engine = RuleEngine(load_rules(path))
    bus = Bus("rules")
    lock = threading.Lock()

    def on_value(topic: str, message: Message) -> None:
        with lock:
            if topic not in engine.topics:
                return
            outputs = engine.on_message(topic, message)
        for out_topic, out_message, retain in outputs:
            bus.publish(out_topic, out_message, retain=retain)

    def announce(state: str, detail: str) -> None:
        extra = {"rules": [r.id for r in engine.rules], "detail": detail}
        bus.publish(f"{uns.SERVICES}/rules/config", Message(state, extra=extra), True)

    # Everything under the site, filtered by the engine: an edited file can watch new
    # topics without resubscribing. A retained value may be hours old: live values only.
    bus.subscribe(f"{uns.SITE}/#", on_value, retained=False)
    stop = stop_on_signal()
    bus.start()
    log.info("%d rules loaded, watching %d topics", len(engine.rules), len(engine.topics))
    announce("cargadas", str(path))

    # The file is the source of truth: when it changes (the console saves it), reload.
    # A broken file is refused and the rules that were running keep running.
    seen = path.stat().st_mtime
    while not stop.wait(2):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime == seen:
            continue
        seen = mtime
        try:
            rules = load_rules(path)
        except (RuleError, yaml.YAMLError, OSError) as exc:
            log.error("rules file not reloaded, keeping the previous rules: %s", exc)
            announce("error", str(exc))
            continue
        with lock:
            engine.replace(rules)
        log.info("rules reloaded: %d rules, %d topics", len(rules), len(engine.topics))
        announce("recargadas", f"{len(rules)} reglas")
    bus.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
