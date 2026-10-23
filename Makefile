# Scheduler · atajos. `make` a secas enseña la lista.
# Funciona con el make 3.81 que trae macOS: nada de extensiones modernas.

SHELL := /bin/bash
PY := ./.venv/bin/python
COMPOSE := docker compose

# Un valor del .env sin ejecutarlo (como run.sh).
env_get = $(shell sed -n 's/^$(1)=//p' .env 2>/dev/null | tail -1 | tr -d "\"' \r")
PORT ?= $(or $(call env_get,PORT),8787)
OLLAMA_PULL ?= $(or $(call env_get,OLLAMA_MODEL),gemma4:26b)

# Docker escribe en data/ y media/ con tu usuario, no con root.
export APP_UID := $(shell id -u)
export APP_GID := $(shell id -g)

.DEFAULT_GOAL := help
.PHONY: help bootstrap setup dev doctor models ollama run demo backup snapshot restore \
	test lint check docker-check docker-build docker-up docker-down docker-logs docker-shell \
	docker-test docker-demo clean

help: ## Esta lista
	@awk 'BEGIN {FS = ":.*## "} \
		/^##@/ {printf "\n\033[1m%s\033[0m\n", substr($$0, 5)} \
		/^[a-zA-Z_-]+:.*## / {printf "  \033[36mmake %-14s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)
	@echo

##@ Mac nuevo

bootstrap: ## Instala Python 3.13, ffmpeg y Ollama con Homebrew, y prepara la app
	@command -v brew >/dev/null 2>&1 || { echo "✗ Falta Homebrew: https://brew.sh (una línea en la Terminal)"; exit 1; }
	brew bundle --file=Brewfile
	@$(MAKE) --no-print-directory setup
	@echo "▸ Ollama: brew services start ollama  y luego  make ollama  (opcional)"
	@echo "▸ Siguiente: make doctor  y  make run"

setup: ## Entorno virtual, dependencias y .env (no arranca nada)
	@./run.sh --setup

dev: setup .venv/.dev.stamp ## setup + herramientas de desarrollo (pytest, ruff)

.venv/.dev.stamp: requirements-dev.txt requirements.txt
	$(PY) -m pip install --quiet -r requirements-dev.txt
	@touch $@

doctor: ## Diagnóstico: Python, ffmpeg, base de datos, clave, Ollama, red, puerto
	@if [ -x $(PY) ]; then $(PY) tools/doctor.py $(ARGS); else python3 tools/doctor.py $(ARGS); fi

models: setup ## Descarga ya los pesos de Whisper (~460 MB), para no esperar al primer vídeo
	$(PY) -c "from app.config import get_settings; from faster_whisper import download_model; \
		s = get_settings(); print(download_model(s.whisper_model, cache_dir=str(s.whisper_models_dir)))"

ollama: ## Descarga el modelo de Ollama del .env (o gemma4:26b, ~17 GB)
	@command -v ollama >/dev/null 2>&1 || { echo "✗ Falta Ollama: brew install ollama && brew services start ollama"; exit 1; }
	ollama pull $(OLLAMA_PULL)

##@ Día a día

run: ## Arranca la app (http://127.0.0.1:8787, o el PORT del .env)
	@./run.sh

demo: setup ## La app contra un Zernio y un Ollama falsos, con sus datos aparte (puerto 8790)
	@./tools/demo.sh

backup: setup ## Copia de la base de datos a data/backups
	@$(PY) -c "from app.services import backups; n, b = backups.create(); \
		print(f'✓ data/backups/{n} ({b / 1024 / 1024:.1f} MB)')"

snapshot: setup ## Para otro Mac: base de datos + clave de cifrado + .env + hashtags, en un archivo
	@$(PY) tools/snapshot.py create

restore: setup ## Restaura una instantánea (app cerrada): make restore FILE=data/backups/snapshot-….tar.gz
	@test -n "$(FILE)" || { echo "Uso: make restore FILE=data/backups/snapshot-AAAAMMDD-HHMMSS.tar.gz"; exit 1; }
	@$(PY) tools/snapshot.py restore "$(FILE)"

##@ Calidad

test: dev ## Tests (pytest, con un Zernio falso: nada sale a internet)
	$(PY) -m pytest -q $(ARGS)

lint: dev ## Errores de código (ruff; ver ruff.toml)
	$(PY) -m ruff check app tools tests

check: lint test ## lint + tests

##@ Docker (opcional: VPS o equipo siempre encendido, entorno fijado)

docker-check:
	@command -v docker >/dev/null 2>&1 || { echo "✗ Docker no está instalado: brew install --cask orbstack (o Docker Desktop)"; exit 1; }
	@docker info >/dev/null 2>&1 || { echo "✗ El motor de Docker no está en marcha: abre OrbStack o Docker Desktop"; exit 1; }

docker-build: docker-check ## Construye la imagen
	$(COMPOSE) build app

docker-up: docker-check ## Arranca en segundo plano, con los mismos data/, media/ y .secrets/
	@if [ -z "$$($(COMPOSE) ps --status running -q app 2>/dev/null)" ] \
		&& lsof -nP -iTCP:$(PORT) -sTCP:LISTEN >/dev/null 2>&1; then \
		echo "✗ El puerto $(PORT) está ocupado: ¿la app nativa está abierta? Ciérrala antes (no pueden publicar las dos)."; \
		exit 1; fi
	$(COMPOSE) up -d --build app
	@echo "▸ http://127.0.0.1:$(PORT)  ·  logs: make docker-logs  ·  parar: make docker-down"

docker-down: ## Para el contenedor
	$(COMPOSE) down

docker-logs: ## Sigue los logs del contenedor
	$(COMPOSE) logs -f app

docker-shell: ## Una terminal dentro del contenedor
	$(COMPOSE) exec app bash

docker-test: docker-check ## Los tests dentro de la imagen (Linux)
	docker build --target test -t scheduler:test .
	docker run --rm scheduler:test

docker-demo: docker-check ## La demo en Docker (Zernio y Ollama falsos): http://127.0.0.1:8790
	$(COMPOSE) --profile demo up --build mock-zernio demo

##@ Limpieza

clean: ## Borra cachés de Python y de las herramientas (no toca datos ni vídeos)
	find . -path ./.venv -prune -o -name __pycache__ -type d -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache
