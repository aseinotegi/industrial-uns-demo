#!/bin/sh
# Grafana reads the history with its own user, which can only SELECT: a dashboard can
# never change or delete what the historian wrote.
set -eu
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<SQL
CREATE ROLE grafana LOGIN PASSWORD '${GRAFANA_DB_PASSWORD}';
GRANT CONNECT ON DATABASE ${POSTGRES_DB} TO grafana;
GRANT USAGE ON SCHEMA public TO grafana;
ALTER DEFAULT PRIVILEGES FOR ROLE ${POSTGRES_USER} IN SCHEMA public GRANT SELECT ON TABLES TO grafana;
SQL
