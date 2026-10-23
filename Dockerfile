# syntax=docker/dockerfile:1
# Scheduler en un contenedor: el mismo código con Python y ffmpeg fijados.
#
#   make docker-up      la app, con data/, media/ y .secrets/ del proyecto
#   make docker-test    los tests dentro de la imagen (target «test»)
#
# Ollama NO va dentro. En un Mac, Docker no ve la GPU (Metal): un modelo de
# 17 GB en la CPU de la máquina virtual tarda minutos por texto. El contenedor
# usa el Ollama del propio Mac a través de host.docker.internal (docker-compose.yml).

# La que se usa a diario; la mínima que se promete es 3.11
# (docker build --build-arg PYTHON_VERSION=3.11 --target test .).
ARG PYTHON_VERSION=3.13
FROM python:${PYTHON_VERSION}-slim-bookworm AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore \
    # Cualquier UID puede ejecutarlo (compose usa el tuyo): HOME tiene que ser escribible.
    HOME=/tmp \
    TZ=Europe/Madrid

# ffmpeg: ffprobe valida cada vídeo antes de subirlo. tzdata: la hora de los logs.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install -r requirements.txt

# --------------------------------------------------------------------------- #
FROM base AS app

COPY app ./app
COPY tools ./tools
# El banco de temas de los hashtags. Con data/ montado desde el proyecto manda
# el tuyo (lo puedes editar); éste es el de partida.
COPY data/hashtags.json ./data/hashtags.json

# Sin root por defecto; docker-compose.yml lo sustituye por tu UID para que lo
# que escribe en data/ y media/ sea tuyo.
# Las carpetas de datos, suyas: un volumen con nombre (la demo) hereda este dueño.
RUN useradd --uid 1000 --create-home --home-dir /home/app app \
    && mkdir -p /app/data /app/media/library /app/.secrets \
    && chown -R app:app /app/data /app/media /app/.secrets
USER app

# Dentro del contenedor escucha en todas las interfaces; lo que decide quién
# llega es el puerto publicado (127.0.0.1 en docker-compose.yml).
ENV HOST=0.0.0.0 \
    PORT=8787
EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/health' % os.environ.get('PORT', '8787'), timeout=4)"]

CMD ["python", "-m", "app.main"]

# --------------------------------------------------------------------------- #
FROM app AS test

USER root
COPY requirements-dev.txt pytest.ini ruff.toml ./
COPY tests ./tests
RUN pip install -r requirements-dev.txt
USER app
# /app es de root: sin caché de pytest, que sólo avisaría de que no puede escribirla.
CMD ["sh", "-c", "python -m ruff check --no-cache app tools tests && python -m pytest -q -p no:cacheprovider"]
