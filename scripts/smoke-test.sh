#!/usr/bin/env bash
# End-to-end check used by CI: start the whole demo with the story sped up and wait
# until the database shows the full chain: alarm confirmed → order → valve closed.
set -euo pipefail
cd "$(dirname "$0")/.."

export WORLD_TIME_SCALE=0.25 ROBOT_ROUND_S=4 ROBOT_TRAVEL_S=2.5
docker compose up -d --build

sql() { docker compose exec -T timescaledb psql -U uns -d uns -tAc "$1" 2>/dev/null || true; }
seen() { [ "$(sql "SELECT count(*) FROM events WHERE topic = '$1' AND value = '$2'")" -ge 1 ] 2>/dev/null; }

echo "waiting for the story to happen..."
for _ in $(seq 1 60); do
  if seen planta1/exterior/reglas/sobrepresion-linea-104 activa \
    && seen planta1/exterior/xv-104/orden cerrar \
    && seen planta1/exterior/xv-104/estado cerrada \
    && seen planta1/exterior/reglas/sobrepresion-linea-104 normal; then
    echo "✓ alarm confirmed, order sent, valve closed, alarm cleared"
    echo "✓ PLC values stored: $(sql "SELECT count(*) FROM measurements WHERE topic = 'planta1/exterior/pt-104/presion'")"
    curl -fsS http://localhost:3000/api/dashboards/uid/linea-104 > /dev/null && echo "✓ Grafana dashboard provisioned"
    docker compose run --rm tree
    exit 0
  fi
  sleep 5
done

echo "✗ the story did not complete in 5 minutes" >&2
docker compose logs --tail 40
exit 1
