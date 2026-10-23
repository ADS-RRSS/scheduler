#!/usr/bin/env bash
# Arranca Scheduler. Crea el entorno virtual la primera vez.
#   ./run.sh           prepara lo que falte y arranca
#   ./run.sh --setup   sólo prepara (entorno, dependencias, .env) y sale
set -euo pipefail
cd "$(dirname "$0")"

SETUP_ONLY=0
[ "${1:-}" = "--setup" ] && SETUP_ONLY=1

# Un Python 3.11 o superior. En un Mac recién estrenado «python3» es el 3.9 de
# las herramientas de Xcode, con el que la app ni arranca: se busca uno mejor
# antes de crear el entorno, y un entorno hecho con uno viejo (o con un Python
# que brew ya desinstaló) se vuelve a crear.
is_modern() {
  "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1
}

find_python() {
  local candidate
  for candidate in "${PYTHON:-}" python3.13 python3.12 python3.11 \
      /opt/homebrew/bin/python3 /usr/local/bin/python3 python3; do
    [ -n "$candidate" ] || continue
    command -v "$candidate" >/dev/null 2>&1 || continue
    if is_modern "$candidate"; then
      command -v "$candidate"
      return 0
    fi
  done
  return 1
}

if ! { [ -x .venv/bin/python ] && is_modern .venv/bin/python; }; then
  if [ -d .venv ]; then
    echo "▸ El entorno virtual está roto o usa un Python anterior a 3.11: se vuelve a crear."
    rm -rf .venv
  fi
  if ! PY="$(find_python)"; then
    echo "✗ Hace falta Python 3.11 o superior (python3 aquí es: $(python3 --version 2>&1 || echo 'ninguno'))." >&2
    echo "  En un Mac:  brew install python@3.13   (o  make bootstrap , que instala todo)" >&2
    exit 1
  fi
  echo "▸ Creando entorno virtual con $("$PY" --version 2>&1)…"
  "$PY" -m venv .venv
  ./.venv/bin/python -m pip install --quiet --upgrade pip
  rm -f .venv/.requirements.stamp
fi

# Dependencias nuevas tras una actualización: se instalan cuando
# requirements.txt es más reciente que la última instalación.
if [ ! -f .venv/.requirements.stamp ] || [ requirements.txt -nt .venv/.requirements.stamp ]; then
  echo "▸ Instalando dependencias…"
  ./.venv/bin/python -m pip install --quiet -r requirements.txt
  touch .venv/.requirements.stamp
fi

if [ ! -f .env ]; then
  cp .env.example .env
  echo "▸ Se ha creado .env a partir de .env.example (los valores por defecto sirven)."
fi

if ! command -v ffprobe >/dev/null 2>&1; then
  echo "⚠ ffmpeg no está instalado: los vídeos se subirán sin validar (brew install ffmpeg)."
fi

[ "$SETUP_ONLY" = 1 ] && { echo "▸ Listo. Arranca con  ./run.sh  o  make run"; exit 0; }

# Lee un valor del .env sin llegar a ejecutarlo (evita sorpresas con comillas).
read_env() {
  grep -E "^$1=" .env 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"'"'"' \r' || true
}

PORT="$(read_env PORT)"; PORT="${PORT:-8787}"
# Una copia ya abierta en otra terminal (o en Docker) publicaría lo mismo: la
# app también se niega, pero aquí el aviso llega antes y más claro.
if command -v lsof >/dev/null 2>&1 && lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "✗ Ya hay algo escuchando en el puerto $PORT: ¿la app ya está abierta (en otra terminal o en Docker)?" >&2
  echo "  Ciérrala antes, o cambia PORT en .env si es otra cosa." >&2
  exit 1
fi

echo "▸ Abriendo http://127.0.0.1:${PORT}"
exec ./.venv/bin/python -m app.main
