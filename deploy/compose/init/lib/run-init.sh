#!/bin/sh
# Kubernetes db-bootstrap Job: run every init file in order (platform LLD §2.1, §9.3).
# Lives in lib/ because the Postgres image entrypoint executes any *.sh in docker-entrypoint-initdb.d.
set -eu
cd "$(dirname "$0")/.."   # lib/ is skipped by the Postgres image entrypoint, which runs every *.sh in initdb.d
for f in [0-9][0-9]-*.sql; do
  if [ "$f" = "40-bank-stub.sql" ] && [ "${SKIP_BANK_STUB:-0}" = "1" ]; then continue; fi
  echo "bootstrap: $f"
  psql -v ON_ERROR_STOP=1 -X -q -f "$f"
done
