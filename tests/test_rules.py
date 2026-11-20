from pathlib import Path

import pytest

from uns_demo import uns
from uns_demo.rules import RuleEngine, RuleError, load_rules, parse_rules
from uns_demo.uns import Message

RULES_FILE = Path(__file__).resolve().parents[1] / "config" / "rules.yaml"
ALARM = f"{uns.RULES}/sobrepresion-linea-104"
EXT_12 = "planta1/exterior/ext-12/estado"


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def engine():
    clock = Clock()
    return RuleEngine(load_rules(RULES_FILE), clock=clock), clock


def vision(value, quality="good"):
    return Message(value, unit="bar", source="vision", quality=quality)


def plc(value):
    return Message(value, unit="bar")


def topics(outputs):
    return [t for t, _, _ in outputs]


def test_the_shipped_rules_file_is_valid():
    rules = load_rules(RULES_FILE)
    assert [r.id for r in rules] == ["sobrepresion-linea-104", "extintor-ext-12"]
    assert [r.mode for r in rules] == ["act", "notify"]


def test_fires_only_when_vision_and_plc_agree(engine):
    eng, _ = engine
    assert eng.on_message(uns.PT_104, plc(6.7)) == []  # the PLC alone is not enough
    out = eng.on_message(uns.PI_104, vision(6.8))
    assert topics(out) == [ALARM, uns.XV_104_ORDER]
    alarm, order = out[0][1], out[1][1]
    assert alarm.value == "activa" and alarm.extra["inputs"] == {uns.PI_104: 6.8, uns.PT_104: 6.7}
    assert order.value == "cerrar" and out[1][2] is False  # orders are never retained


def test_one_high_reading_and_one_normal_does_not_fire(engine):
    eng, _ = engine
    eng.on_message(uns.PT_104, plc(4.3))
    assert eng.on_message(uns.PI_104, vision(6.8)) == []


def test_stale_values_do_not_count(engine):
    eng, clock = engine
    eng.on_message(uns.PI_104, vision(6.8))
    clock.t += 60  # older than max_age_s: 45
    assert eng.on_message(uns.PT_104, plc(6.7)) == []


def test_uncertain_photos_do_not_count(engine):
    eng, _ = engine
    eng.on_message(uns.PT_104, plc(6.7))
    assert eng.on_message(uns.PI_104, vision(6.8, quality="uncertain")) == []


def test_fires_once_until_cleared_with_hysteresis(engine):
    eng, _ = engine
    eng.on_message(uns.PT_104, plc(6.7))
    assert eng.on_message(uns.PI_104, vision(6.8))
    assert eng.on_message(uns.PI_104, vision(6.9)) == []  # still active: no second order
    assert eng.on_message(uns.PT_104, plc(5.8)) == []  # below 6.0 but above 5.5: still active
    out = eng.on_message(uns.PT_104, plc(5.2))
    assert topics(out) == [ALARM] and out[0][1].value == "normal"
    # the vision reading is still recent, so the next high PLC value is enough to fire again
    assert topics(eng.on_message(uns.PT_104, plc(6.6))) == [ALARM, uns.XV_104_ORDER]


def test_notify_mode_raises_the_alarm_without_orders():
    rules = parse_rules(
        {
            "rules": [
                {
                    "id": "aviso",
                    "mode": "notify",
                    "when": [{"topic": "a/b", "above": 1}],
                    "clear_when": [{"topic": "a/b", "below": 0}],
                    "on_trigger": [{"topic": "a/orden", "value": "cerrar"}],
                }
            ]
        }
    )
    out = RuleEngine(rules, clock=Clock()).on_message("a/b", Message(2))
    assert topics(out) == [f"{uns.RULES}/aviso"]


def test_extinguisher_not_ok_raises_a_notice_and_ok_clears_it(engine):
    eng, _ = engine
    out = eng.on_message(EXT_12, Message("NOK", source="inspector"))
    assert topics(out) == [f"{uns.RULES}/extintor-ext-12"] and out[0][1].value == "activa"
    assert eng.on_message(EXT_12, Message("NOK", source="inspector")) == []  # once until cleared
    out = eng.on_message(EXT_12, Message("OK", source="inspector"))
    assert out[0][1].value == "normal"


def test_an_uncertain_extinguisher_photo_does_not_raise_the_notice(engine):
    eng, _ = engine
    assert eng.on_message(EXT_12, Message("NOK", quality="uncertain")) == []


def test_text_values_never_satisfy_numeric_conditions(engine):
    eng, _ = engine
    eng.on_message(uns.PT_104, plc(6.7))
    assert eng.on_message(uns.PI_104, Message("error")) == []


def test_subscribes_only_to_the_topics_it_needs(engine):
    eng, _ = engine
    assert eng.topics == {uns.PI_104, uns.PT_104, EXT_12}


@pytest.mark.parametrize(
    "rule, message",
    [
        ({"when": [{"topic": "a", "above": 1}], "clear_when": [{"topic": "a", "below": 0}]}, "id"),
        ({"id": "Mala Regla", "when": [], "clear_when": []}, "lowercase"),
        ({"id": "r", "mode": "maybe", "when": [{"topic": "a", "above": 1}], "clear_when": []}, "mode"),
        ({"id": "r", "when": [{"topic": "a", "above": 1}]}, "clear_when"),
        ({"id": "r", "when": [{"topic": "a", "above": 1, "below": 2}], "clear_when": []}, "exactly one"),
        ({"id": "r", "when": [{"topic": "a", "equals": "x", "above": 2}], "clear_when": []}, "exactly one"),
        ({"id": "r", "when": [{"topic": "a", "abov": 1}], "clear_when": []}, "exactly one"),
        ({"id": "r", "when": [{"topic": "a", "above": 1, "age": 3}], "clear_when": []}, "unknown keys"),
    ],
)
def test_invalid_rules_are_refused_at_start(rule, message):
    with pytest.raises(RuleError, match=message):
        parse_rules({"rules": [rule]})


def test_duplicated_ids_are_refused():
    rule = {"id": "r", "when": [{"topic": "a", "above": 1}], "clear_when": [{"topic": "a", "below": 0}]}
    with pytest.raises(RuleError, match="unique"):
        parse_rules({"rules": [rule, dict(rule)]})
