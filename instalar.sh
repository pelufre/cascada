#!/usr/bin/env bash
# Instala o actualiza Cascada en un servidor Ubuntu/Debian (Hetzner). Correr como root dentro de la carpeta del repo:
#   ./instalar.sh
set -euo pipefail
cd "$(dirname "$0")"

say(){ printf '\n\033[1m%s\033[0m\n' "$*"; }

if [ "$(id -u)" -ne 0 ]; then echo "Correr como root (o con sudo)"; exit 1; fi

say "1/6 Docker"
if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sh
fi
docker compose version >/dev/null

say "2/6 Cortafuegos (SSH, HTTP, HTTPS)"
if command -v ufw >/dev/null; then
  ufw allow OpenSSH >/dev/null; ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null
  yes | ufw enable >/dev/null || true
  ufw status | head -8
fi

say "3/6 Archivo .env"
if [ ! -f .env ]; then cp .env.ejemplo .env; fi
chmod 600 .env
if ! grep -q '^WEB_CLAVE=.\+' .env; then
  CL=$(openssl rand -hex 12)
  sed -i "s/^WEB_CLAVE=.*/WEB_CLAVE=$CL/" .env
fi
if ! grep -q '^DOMINIO=.\+' .env; then
  IP=$(curl -fsS -4 --max-time 10 https://ifconfig.me || true)
  [ -n "$IP" ] || IP=$(hostname -I | awk '{print $1}')
  sed -i "s/^DOMINIO=.*/DOMINIO=${IP//./-}.sslip.io/" .env
fi
mkdir -p datos caddy_data caddy_config

FALTA=""
grep -q '^CMC_API_KEY=.\+' .env || FALTA="$FALTA CMC_API_KEY"
grep -q '^TELEGRAM_TOKEN=.\+' .env || FALTA="$FALTA TELEGRAM_TOKEN"
if [ -n "$FALTA" ]; then
  say "Faltan datos en .env:$FALTA"
  echo "Editalo con:  nano .env   (guardar: Ctrl+O, Enter; salir: Ctrl+X) y volvé a correr ./instalar.sh"
  exit 0
fi

say "4/6 Construyendo y arrancando"
docker compose up -d --build

say "5/6 Verificación"
sleep 8
docker compose exec -T cascada python -m cascada.cli verificar || true

if ! grep -q '^TELEGRAM_CHAT_ID=.\+' .env; then
  say "Falta TELEGRAM_CHAT_ID"
  echo "Escribile cualquier cosa a tu bot en Telegram y corré:"
  echo "  docker compose exec cascada python -m cascada.cli telegram-chat"
  echo "Copiá el número en TELEGRAM_CHAT_ID dentro de .env y corré: docker compose up -d"
fi

say "6/6 Listo"
D=$(grep '^DOMINIO=' .env | cut -d= -f2); U=$(grep '^WEB_USUARIO=' .env | cut -d= -f2); C=$(grep '^WEB_CLAVE=' .env | cut -d= -f2)
echo "Web:      https://$D   (el certificado tarda ~1 minuto la primera vez)"
echo "Usuario:  $U"
echo "Clave:    $C"
echo "Registros: docker compose logs -f cascada"
