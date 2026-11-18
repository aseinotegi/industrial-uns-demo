# Industrial UNS demo

[![CI](https://github.com/aseinotegi/industrial-uns-demo/actions/workflows/ci.yml/badge.svg)](https://github.com/aseinotegi/industrial-uns-demo/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13-blue)
![License](https://img.shields.io/badge/license-MIT-green)

**English** · [Español](README.es.md)

A **Unified Namespace** for a small plant, running on your laptop with one command. A robot's photo of a gauge and a PLC transmitter publish into the same hierarchy, a rule cross-checks them, the robot closes a manual valve and everything ends up in a historian with a dashboard.

It is the story told on [andersein.com](https://andersein.com), working: no hardware, no cloud, real protocols.

```mermaid
flowchart LR
    subgraph plant["Plant (simulated)"]
        W["world<br/>pressure line · PT-104 · PI-104 · XV-104"]
    end
    R["robot<br/>rounds and missions"] -- "photo of PI-104" --> B
    W -- "S7 · DB1" --> G["plc-gateway<br/>(plc-mqtt-gateway)"]
    G -- "pt-104/presion" --> B
    B[("UNS<br/>MQTT broker<br/>planta1/…")]
    B -- photo --> V["vision"]
    V -- "pi-104/presion" --> B
    B --> RU["rules"]
    RU -- "alarm · xv-104/orden" --> B
    B -- order --> R
    R -- "closes XV-104" --> W
    B --> H["historian"] --> T[("TimescaleDB")] --> GR["Grafana"]
```

## Quick start

```bash
docker compose up --build
```

Open **http://localhost:3000** (Grafana, no login). Every three minutes the line pressure rises from 4.2 towards 6.8 bar. Within a minute you will see the alarm confirmed by both sources, the robot closing XV-104 and the pressure falling back.

Too slow for a first look? `WORLD_TIME_SCALE=0.25 ROBOT_ROUND_S=4 docker compose up --build` runs the whole story in under a minute.

The namespace, right now:

```text
$ docker compose run --rm tree
planta1/
  exterior/
    pi-104/
      presion                 6.40 bar   (vision, 95%)
    pt-104/
      alarma-alta             True
      presion                 6.00 bar
    reglas/
      sobrepresion-linea-104  activa   (rules)
    xv-104/
      estado                  cerrada   (robot-01)
  robots/
    robot-01/
      mision                  completada   (robot-01)
  servicios/
    historian                 online   (historian)
    ...
```

And what the historian keeps, one alarm from start to end (real output of a run):

```text
16:28:54.920 | planta1/robots/robot-01/foto                   | IMG_0413
16:28:54.924 | planta1/exterior/reglas/sobrepresion-linea-104 | activa
16:28:54.924 | planta1/exterior/xv-104/orden                  | cerrar
16:28:54.926 | planta1/robots/robot-01/mision                 | en-camino
16:28:57.428 | planta1/exterior/xv-104/estado                 | cerrada
16:28:57.429 | planta1/robots/robot-01/mision                 | completada
16:28:58.381 | planta1/exterior/reglas/sobrepresion-linea-104 | normal
```

## The story, service by service

| Step | Service | What it does |
|---|---|---|
| 1 | `robot` | Every round it stops at local gauge **PI-104** (a dial with no signal) and takes a photo |
| 2 | `vision` | Reads the photo: class, confidence and value. Bad photos are published as `quality: uncertain` |
| 3 | `plc-gateway` | Reads transmitter **PT-104** from the Siemens S7 PLC and publishes it to its UNS path |
| 4 | `rules` | Vision **and** PLC above 6 bar, both recent → alarm confirmed, order `cerrar` for XV-104 |
| 5 | `robot` | Goes to manual valve **XV-104** (no actuator) and closes it. Reports the new position |
| 6 | `historian` | Stores every value and event in TimescaleDB. Grafana shows the peak, the alarm and the closing |

`world` plays the physical plant: the pressure line, the PLC (a real S7 server on port 1102), the gauge face and the valve. It is the only thing that is not software in real life.

## The namespace

One hierarchy, `site/area/device/value`, named like the plant's P&ID (in Spanish, as in the plant; the code is in English):

| Topic | Published by | Retained | Example value |
|---|---|---|---|
| `planta1/exterior/pi-104/presion` | vision | yes | `6.8` bar, confidence 0.98 |
| `planta1/exterior/pt-104/presion` | plc-gateway | yes | `6.71` bar |
| `planta1/exterior/pt-104/alarma-alta` | plc-gateway | yes | `true` (the PLC's own bit) |
| `planta1/exterior/reglas/<rule>` | rules | yes | `activa` / `normal`, with the inputs that decided |
| `planta1/exterior/xv-104/orden` | rules | **no** | `cerrar` |
| `planta1/exterior/xv-104/estado` | robot | yes | `abierta` / `cerrada` |
| `planta1/robots/robot-01/foto` | robot | no | `IMG_0412` |
| `planta1/robots/robot-01/mision` | robot | yes | `en-camino` / `completada` |
| `planta1/servicios/<service>` | each service | yes | `online` / `offline` (Last Will) |

Every message follows one contract. Only `value` is mandatory, extra fields are kept:

```json
{"value": 6.8, "unit": "bar", "ts": "2026-11-18T15:28:54.920Z",
 "source": "vision", "quality": "good", "confidence": 0.98, "image": "IMG_0413"}
```

## Rules

Plain YAML, readable by whoever runs the plant ([config/rules.yaml](config/rules.yaml)):

```yaml
- id: sobrepresion-linea-104
  mode: act                 # notify: alarm only · act: alarm + orders
  when:                     # all of them, each one recent enough
    - { topic: planta1/exterior/pi-104/presion, above: 6.0, max_age_s: 45, quality: good }
    - { topic: planta1/exterior/pt-104/presion, above: 6.0, max_age_s: 30 }
  clear_when:               # hysteresis: clears below 5.5, not 6.0
    - { topic: planta1/exterior/pt-104/presion, below: 5.5 }
  on_trigger:
    - { topic: planta1/exterior/xv-104/orden, value: cerrar }
```

## Design decisions

- **The UNS is the only interface.** No service calls another. Vision does not know the rules exist; the rules do not know the value came from a camera. Adding a second robot or a third plant means publishing under a new branch.
- **Two sources before acting.** A single bad photo or a noisy transmitter never sends a robot. Uncertain photos are published (the dashboard can show them) but rules require `quality: good`.
- **Freshness is measured by the receiver.** `max_age_s` uses the rule engine's clock at arrival, not the timestamp in the payload, so a device with a wrong clock cannot fake a recent value. Retained values are history, so they never trigger a rule.
- **Orders are not retained.** States are (a late subscriber gets the current picture), orders are not: a robot that restarts must not replay yesterday's `cerrar`.
- **Fire once, clear with margin.** An active rule does not fire again until it clears, and it clears at 5.5 bar, not 6.0. No order storms around the threshold.
- **Notify first, act later.** Same file, one word. That is how automatic actions earn trust in a plant.
- **Reuse, not copy.** The PLC side is [plc-mqtt-gateway](https://github.com/aseinotegi/plc-mqtt-gateway), built straight from its repository. Its per-tag `topic` option puts the PLC value at its UNS path.
- **Historian that survives restarts.** Batched inserts (one transaction per second), rows kept in memory while the database restarts, schema created on start. TimescaleDB hypertables when available, plain PostgreSQL otherwise.

## Project layout

```text
src/uns_demo/
  uns.py        the namespace and the message contract
  bus.py        MQTT for every service: Last Will status, retained or live-only subscriptions
  world.py      simulated plant: physics, S7 PLC (DB1) and the robot's view of gauge and valve
  vision.py     photo → reading, with confidence and quality
  robot.py      rounds and missions
  rules.py      YAML rules, pure engine (messages in, messages out)
  historian.py  UNS → TimescaleDB, batched and resilient
  tree.py       prints the namespace
config/         rules, gateway tags, broker
grafana/        datasource and dashboard, provisioned
scripts/        end-to-end smoke test (used by CI)
```

## Tests

```bash
pip install -e ".[dev]"
pytest -q                  # 52 tests, no broker or database needed
./scripts/smoke-test.sh    # the whole stack in Docker, checked end to end
```

CI runs the unit tests on Python 3.10–3.13 and then the full `docker compose` stack, waiting until the database shows the whole chain: alarm, order, valve closed, alarm cleared.

## Part of a series

| Repository | Role in the story |
|---|---|
| [plc-mqtt-gateway](https://github.com/aseinotegi/plc-mqtt-gateway) | PLC → MQTT, used here for PT-104 |
| [robot-fleet-watchdogs](https://github.com/aseinotegi/robot-fleet-watchdogs) | Rules and watchdogs for a real robot fleet |
| [andersein.com](https://andersein.com) | The story, drawn |

---

MIT License · [Ander Sein](https://andersein.com) · [aseinotegi@gmail.com](mailto:aseinotegi@gmail.com)
