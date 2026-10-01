#!/usr/bin/env bash
# Copia diaria de la base (programar con: crontab -e  →  15 3 * * * /root/cascada/scripts/respaldo.sh)
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p respaldos
for m in papel real; do
  [ -f "datos/cascada_$m.db" ] || continue
  docker compose exec -T cascada python -c "import sqlite3; s=sqlite3.connect('/app/datos/cascada_$m.db'); d=sqlite3.connect('/app/datos/respaldo.db'); s.backup(d); d.close()"
  mv datos/respaldo.db "respaldos/cascada_${m}_$(date +%F).db"
done
find respaldos -name 'cascada_*.db' -mtime +30 -delete
