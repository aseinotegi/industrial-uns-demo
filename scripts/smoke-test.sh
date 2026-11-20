#!/usr/bin/env bash
# End-to-end check used by CI: start the whole demo with the story sped up (x4) and
# wait until the database shows the full chain: alarm confirmed → order → valve closed.
# Then check the security that the README promises.
set -euo pipefail
cd "$(dirname "$0")/.."

export WORLD_TIME_SCALE=0.25 ROBOT_SPEED=6 ROBOT_ACCEL=9.6 ROBOT_DWELL_S=0.75 ROBOT_ACTION_S=1 ROBOT_CHARGE_RATE=12
docker compose up -d --build

sql() { docker compose exec -T timescaledb psql -U uns -d uns -tAc "$1" 2>/dev/null || true; }
seen() { [ "$(sql "SELECT count(*) FROM events WHERE topic = '$1' AND value = '$2'")" -ge 1 ] 2>/dev/null; }
mqtt() { docker compose exec -T broker "$@"; }

echo "waiting for the story to happen..."
for _ in $(seq 1 60); do
  if seen planta1/reglas/sobrepresion-linea-104 activa \
    && seen planta1/exterior/xv-104/orden cerrar \
    && seen planta1/exterior/xv-104/estado cerrada \
    && seen planta1/reglas/sobrepresion-linea-104 normal; then
    echo "✓ alarm confirmed, order sent, valve closed, alarm cleared"
    echo "✓ PLC values stored: $(sql "SELECT count(DISTINCT topic) FROM measurements WHERE source IS NULL OR source = ''") tags"
    echo "✓ real photos inspected: $(sql "SELECT count(DISTINCT topic) FROM events WHERE topic LIKE '%/inspeccion'") points"
    curl -fsS http://localhost:3000/api/dashboards/uid/linea-104 > /dev/null && echo "✓ Grafana dashboard provisioned"
    curl -fsS http://localhost:8000/api/uns | grep -q '"planta1/robots/robot-01/posicion"' && echo "✓ console sees the robot moving"
    curl -fsS http://localhost:8000/ | grep -q '<div id="root">' && echo "✓ console web app served"
    curl -fsS http://localhost:8000/api/planta | grep -q '"ruta"' && echo "✓ console gets the map from the robot"
    curl -fsS http://localhost:8000/api/servicios | python3 -c '
import json, sys
services = {s["nombre"]: s for s in json.load(sys.stdin)["servicios"]}
for name, s in services.items():
    print("  %-10s %-12s %s" % (s["estado"], name, ",".join(s["ips"]) or "aislada"))
assert not services["world"]["visible"] and not services["timescaledb"]["visible"], "segmentation broken"
assert services["broker"]["estado"] == "ok", "broker not reachable from the console"
' && echo "✓ from the console, the PLC and the database are not reachable (segmentation)"

    # -- security ------------------------------------------------------------
    if mqtt mosquitto_sub -t 'planta1/#' -C 1 -W 3 > /dev/null 2>&1; then
      echo "✗ the broker accepted an anonymous client" >&2; exit 1
    fi
    echo "✓ anonymous MQTT refused"
    mqtt sh -c 'mosquitto_sub -u explorer -P explorer -t planta1/exterior/xv-104/orden -C 1 -W 4 > /tmp/got & sleep 1;
                mosquitto_pub -u console -P demo-console -t planta1/exterior/xv-104/orden -m "{\"value\":\"cerrar\"}"; wait; cat /tmp/got' \
      | grep -q cerrar && { echo "✗ the console could send an order to the plant" >&2; exit 1; }
    echo "✓ the console cannot send orders (ACL)"
    [ "$(docker compose exec -T timescaledb psql -U grafana -d uns -tAc 'SELECT count(*) > 0 FROM measurements')" = "t" ] \
      && echo "✓ Grafana's database user can read the history"
    if docker compose exec -T timescaledb psql -U grafana -d uns -tAc 'DELETE FROM measurements' > /dev/null 2>&1; then
      echo "✗ Grafana's database user can delete history" >&2; exit 1
    fi
    echo "✓ Grafana's database user cannot change it"
    docker compose run --rm tree | head -40
    exit 0
  fi
  sleep 5
done

echo "✗ the story did not complete in 5 minutes" >&2
docker compose logs --tail 40
exit 1
