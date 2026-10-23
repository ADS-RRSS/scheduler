#!/usr/bin/env bash
# Arranca la app contra un Zernio FALSO para poder probarlo todo sin cuenta real.
# Nada sale a internet. Ctrl-C para parar ambos procesos.
#
# Todo lo de la demo vive en data/demo/ (base de datos, biblioteca, copias): la
# biblioteca de verdad (media/library) es la carpeta vigilada de la app real, y
# la demo la registraría entera. El Ollama también es el falso del mock, así que
# «Proponer con IA» responde sin tener Ollama instalado.
set -euo pipefail
cd "$(dirname "$0")/.."

PY=./.venv/bin/python
if [ ! -x "$PY" ]; then
  echo "▸ Falta el entorno: ejecuta primero  make setup  (o ./run.sh una vez)." >&2
  exit 1
fi

DEMO_DIR="$(pwd)/data/demo"
mkdir -p "$DEMO_DIR/library" "$DEMO_DIR/backups"

"$PY" tools/mock_zernio.py &
MOCK_PID=$!
trap 'kill $MOCK_PID 2>/dev/null || true' EXIT
sleep 1

echo "▸ Zernio simulado en   http://127.0.0.1:8899"
echo "▸ App de demo en       http://127.0.0.1:8790   (datos en data/demo/)"
echo "  · «Conectar con Zernio» con la clave  zernio-test-key  lista dos cuentas falsas."
echo "  · Avisos: pon  http://127.0.0.1:8899/notify  en Ajustes y pulsa «Enviar aviso de prueba»."

ZERNIO_API_BASE=http://127.0.0.1:8899/v1 \
OLLAMA_URL=http://127.0.0.1:8899 \
OLLAMA_MODEL= \
DATABASE_URL="sqlite:///$DEMO_DIR/demo.db" \
MEDIA_LIBRARY_DIR="$DEMO_DIR/library" \
BACKUPS_DIR="$DEMO_DIR/backups" \
CAPTION_SHEET_CHECK_SECONDS=0 \
PORT=8790 \
SCHEDULER_TICK_SECONDS=5 \
STATUS_POLL_SECONDS=2 \
"$PY" -m app.main
