#!/usr/bin/env bash
# Instala o actualiza el Sistema 3 en el servidor (Ubuntu/Debian). Correr como root dentro de servidor/:
#   ./instalar.sh
set -euo pipefail
cd "$(dirname "$0")"
say(){ printf '\n\033[1m%s\033[0m\n' "$*"; }
[ "$(id -u)" -eq 0 ] || { echo "Correr como root"; exit 1; }

say "1/5 Docker"
command -v docker >/dev/null || curl -fsSL https://get.docker.com | sh
docker compose version >/dev/null

say "2/5 Cortafuegos (SSH, HTTP, HTTPS)"
if command -v ufw >/dev/null; then
  ufw allow OpenSSH >/dev/null; ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null
  yes | ufw enable >/dev/null || true
fi

say "3/5 Archivo .env"
[ -f .env ] || cp .env.ejemplo .env
chmod 600 .env
grep -q '^WEB_CLAVE=.\+' .env || sed -i "s/^WEB_CLAVE=.*/WEB_CLAVE=$(openssl rand -hex 12)/" .env
if ! grep -q '^DOMINIO=.\+' .env; then
  IP=$(curl -fsS -4 --max-time 10 https://ifconfig.me || hostname -I | awk '{print $1}')
  sed -i "s/^DOMINIO=.*/DOMINIO=${IP//./-}.sslip.io/" .env
fi
mkdir -p datos caddy_data caddy_config
FALTA=""
for v in CMC_API_KEY TELEGRAM_TOKEN; do grep -q "^$v=.\+" .env || FALTA="$FALTA $v"; done
if grep -q '^modo: *real' ../config/sistema3.yaml; then
  for v in KUCOIN_KEY KUCOIN_SECRET KUCOIN_PASSPHRASE; do grep -q "^$v=.\+" .env || FALTA="$FALTA $v"; done
fi
if [ -n "$FALTA" ]; then
  say "Faltan datos en servidor/.env:$FALTA"
  echo "Completalos (nano .env) y volvé a correr ./instalar.sh"
  exit 0
fi

say "4/5 Construyendo y arrancando"
docker compose up -d --build

say "5/5 Verificación"
sleep 8
docker compose exec -T sistema3 python -m sistema3.cli verificar || true
grep -q '^TELEGRAM_CHAT_ID=.\+' .env || echo "Falta TELEGRAM_CHAT_ID: escribile al bot y corré  docker compose exec sistema3 python -m sistema3.cli telegram-chat"
D=$(grep '^DOMINIO=' .env | cut -d= -f2); U=$(grep '^WEB_USUARIO=' .env | cut -d= -f2); C=$(grep '^WEB_CLAVE=' .env | cut -d= -f2)
echo "Web:      https://$D"
echo "Usuario:  $U"
echo "Clave:    $C"
