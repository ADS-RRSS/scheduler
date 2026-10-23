# Instalar, cambiar de Mac y Docker

Todo pasa por `make`. `make` a secas enseña la lista.

| Quiero…                                  | Comando                                              |
|------------------------------------------|------------------------------------------------------|
| Instalarlo en un Mac nuevo               | `make bootstrap`                                     |
| Saber si todo está bien                  | `make doctor`                                        |
| Arrancarlo                               | `make run`                                           |
| Probarlo sin cuenta de Zernio            | `make demo` (o `make docker-demo`)                   |
| Llevarme mi instalación a otro Mac       | `make snapshot` aquí → `make restore FILE=…` allí    |
| Tenerlo siempre encendido (VPS, mini-PC) | `make docker-up`                                     |
| Tests y lint, como CI                    | `make check` (o `make docker-test`, dentro de Linux) |

---

## Un Mac nuevo, paso a paso

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
git clone git@github.com:ADS-RRSS/scheduler.git && cd scheduler
make bootstrap            # Python 3.13, ffmpeg y Ollama (Brewfile) + entorno + .env
brew services start ollama && make ollama   # opcional: el modelo de los textos (~17 GB)
make models               # opcional: los pesos de Whisper ya, en vez de con el primer vídeo
make doctor
make run                  # → http://127.0.0.1:8787
```

**Por qué no basta con `python3`.** El `python3` de un Mac recién estrenado es el
3.9 de las herramientas de Xcode, y la app necesita 3.11 o superior. `./run.sh`
(que es lo que hacen `make setup` y `make run`) busca `python3.13`, `3.12`,
`3.11` o el de Homebrew antes de crear el entorno, y si encuentra un `.venv`
hecho con un Python viejo —o con uno que `brew upgrade` ya se llevó— lo vuelve a
crear. Si no hay ninguno válido, lo dice y sugiere `brew install python@3.13`.

**Los tests prueban las dos puntas**: CI los pasa con Python 3.11 (el mínimo) y
3.13 (el de diario), también dentro de la imagen Docker.

### `make doctor`

Un diagnóstico de medio minuto que no necesita el entorno virtual (sólo la
biblioteca estándar), así que sirve precisamente cuando algo está roto:

```
Python          el del entorno (≥ 3.11), las dependencias, faster-whisper
Herramientas    ffmpeg/ffprobe, Homebrew, Docker (opcional)
Configuración   .env, rutas del .env que no existen aquí, HOST y código de acceso
Base de datos   integridad, esquema, cuentas, vídeos por estado, quién la usa, copias
                la clave de cifrado (y si hay una instantánea para no perderla)
Disco y vídeos  espacio libre, biblioteca, restos de media/inbox|posted|failed
Textos          pesos de Whisper, Ollama y el modelo configurado
Red             API de Zernio y la ruta de subida a Cloudflare R2 (--offline la salta)
Puerto          si la app ya está abierta
```

Cada línea es ✓, ⚠ o ✗, con el arreglo debajo. Sale con código 1 si algo impide
funcionar.

> El Python del instalador de python.org viene **sin certificados raíz** hasta
> ejecutar «Install Certificates.command»: cualquier script con `urllib` falla con
> `CERTIFICATE_VERIFY_FAILED`. La app no se ve afectada (usa `certifi`, como el
> diagnóstico), pero si ves ese error en otro script, es eso.

---

## Llevarte tu instalación a otro Mac

Lo que no se puede rehacer, y dónde está:

| Qué                  | Dónde                     | Si se pierde…                                                   |
|----------------------|---------------------------|-----------------------------------------------------------------|
| La base de datos     | `data/scheduler.db`       | Colas, textos, historial y **qué vídeo es cada post en Zernio** |
| La clave de cifrado  | `.secrets/encryption.key` | Las API keys guardadas no se pueden leer                        |
| Tu configuración     | `.env`                    | Vuelves a los valores por defecto                               |
| El banco de hashtags | `data/hashtags.json`      | Vuelve el del repositorio                                       |
| Los vídeos           | `media/library/`          | Hay que volver a subirlos                                       |

Los cuatro primeros van en **una instantánea**; los vídeos se copian aparte
(son decenas de GB):

```bash
# En el Mac de siempre, con la app cerrada o abierta (la copia es consistente)
make snapshot             # → data/backups/snapshot-AAAAMMDD-HHMMSS.tar.gz (permisos 600)
rsync -aH --info=progress2 media/ nuevo-mac.local:~/scheduler/media/

# En el Mac nuevo (tras make bootstrap)
make restore FILE=~/Downloads/snapshot-AAAAMMDD-HHMMSS.tar.gz
make doctor && make run
```

- `make restore` **se niega con la app abierta**, comprueba las sumas de cada
  archivo, guarda antes lo que va a sustituir (`pre-restore-*.tar.gz`,
  `.env.before-restore`) y borra el `-wal` viejo, que se aplicaría sobre la base
  nueva y la estropearía.
- **La instantánea lleva la clave de cifrado**: guárdala como una contraseña. Si
  una copia de la base de datos llega sin su clave, las colas y los textos se
  conservan pero las API keys hay que pegarlas otra vez en **Cuentas**.
- **No copies `scheduler.db` a mano con la app abierta**: los últimos cambios
  están en `scheduler.db-wal`, y una copia sin él (o con uno de otro momento)
  sale incompleta o corrupta. `make snapshot` y **Ajustes → Crear copia** usan la
  copia en caliente de SQLite.
- En el Mac viejo la biblioteca es un clon APFS de los archivos originales; en el
  nuevo, cada vídeo ocupa lo que ocupa (`df` lo dirá).
- **Cuando el nuevo esté funcionando, no vuelvas a abrir el viejo.** Son dos
  bases de datos distintas con las mismas colas y la misma clave de Zernio: las
  dos publicarían. (Los vídeos ya entregados a Zernio publican solos, abras lo
  que abras.)

---

## Docker: ¿merece la pena?

**En tu Mac, no como forma principal. En un equipo siempre encendido, sí.** Está
montado para las dos cosas, y la instalación nativa y la de Docker comparten los
mismos `data/`, `media/` y `.secrets/`, así que se puede pasar de una a otra.

|                                  | Nativo en el Mac                     | Docker en el Mac                                                           | Docker en un VPS / mini-PC                                      |
|----------------------------------|--------------------------------------|----------------------------------------------------------------------------|-----------------------------------------------------------------|
| Ollama (textos)                  | Con la GPU (Metal): 6–20 s por texto | Usa el Ollama **del Mac** por `host.docker.internal` (Docker no ve la GPU) | Sólo CPU: lento; mejor sin él o en otro equipo                  |
| Whisper                          | Nativo                               | En la máquina virtual: algo más lento                                      | CPU del servidor                                                |
| Instalar                         | `make bootstrap`                     | OrbStack o Docker Desktop, y `make docker-up`                              | Docker y `docker compose up -d`                                 |
| Entorno                          | El del Mac                           | Fijado: Python, ffmpeg, dependencias                                       | Fijado                                                          |
| Publicar con el portátil cerrado | Sólo delegando en Zernio             | Sólo delegando en Zernio                                                   | **Sí**, también el modo local, los comentarios y las analíticas |
| Herramientas de migración        | `library_copy.py` usa clones APFS    | No (Linux)                                                                 | No                                                              |

Lo medido al montarlo: imagen de ~940 MB (Python 3.13 + ffmpeg + faster-whisper),
los 363 tests pasan dentro (con 3.13 y con 3.11), el contenedor usa el Ollama del
Mac con `gemma4:26b` y escribe en `data/` con tu usuario, no con root.

Por qué **no** meter Ollama en la imagen: en macOS, Docker corre en una máquina
virtual Linux sin acceso a la GPU; un modelo de 17 GB en CPU tarda minutos por
texto y ocupa media memoria. El Ollama nativo ya está ahí, más rápido.

Licencias: Docker Desktop es gratis para uso personal y empresas pequeñas;
OrbStack, para uso personal. Revisa las suyas si es para una empresa.

### En el Mac

```bash
make docker-up            # construye y arranca en segundo plano (http://127.0.0.1:8787)
make docker-logs          # los logs
make docker-down          # parar
make docker-shell         # una terminal dentro
make docker-demo          # demo aislada (Zernio y Ollama falsos) en http://127.0.0.1:8790
make docker-test          # lint + tests dentro de Linux
```

- El puerto sólo se publica en **127.0.0.1**: nadie de tu red lo ve.
- El contenedor lee tu `.env`, salvo `HOST`, `PORT`, `OLLAMA_URL` y
  `ALLOWED_HOSTS`, que fija `docker-compose.yml`. No pongas en `.env` rutas
  absolutas del Mac (`DATABASE_URL=sqlite:////Users/…`): dentro no existen.
- Otro Ollama: `DOCKER_OLLAMA_URL=http://otro-equipo:11434 make docker-up`.
- `make docker-up` se niega si la app nativa está abierta en ese puerto.

### Una sola copia a la vez

Dos copias sobre la misma base de datos publicarían cada vídeo dos veces. Por eso:

1. Al arrancar, la app **toma la base de datos** (una fila en `settings` y una
   marca en `data/.scheduler-instance.json`) y la renueva cada 30 s. Otra copia
   —nativa, en Docker o en otra terminal— **se niega a arrancar** mientras esa
   marca esté viva, y dice quién la tiene. La marca en archivo existe porque el
   Mac y la máquina virtual de Docker no comparten la memoria del índice WAL de
   SQLite: uno puede leer filas atrasadas del otro; un archivo sustituido de
   golpe lo ven los dos.
2. Si el puerto está ocupado, la app para **antes** de arrancar el planificador
   (uvicorn abre el puerto después de arrancarlo, y una segunda copia llegaba a
   publicar un momento).
3. Un cierre limpio suelta la base de datos. Tras un corte, la marca caduca a los
   90 s; en el mismo equipo, en cuanto su proceso ya no existe.
   `INSTANCE_TAKEOVER=true` la toma igualmente, y la copia que la pierde deja de
   publicar en su siguiente latido.

### En un VPS o un equipo siempre encendido

Es la única forma de publicar en modo local (y de leer comentarios y analíticas)
con el portátil cerrado. Si sólo quieres cerrar el portátil, **Mandar la cola a
Zernio** ya lo resuelve sin servidor.

```bash
git clone … && cd scheduler
make setup                                   # un entorno sólo para las herramientas
make restore FILE=snapshot-AAAAMMDD-HHMMSS.tar.gz
# desde el Mac:  rsync -aH media/ servidor:~/scheduler/media/
APP_UID=$(id -u) APP_GID=$(id -g) docker compose up -d
```

(`make setup` funciona igual en Linux: busca un Python 3.11 o superior del
sistema. La restauración se hace fuera del contenedor porque `.env` vive en el
proyecto, no dentro de la imagen.)

- **`UI_PASSCODE` obligatorio**, y el puerto en `127.0.0.1`: entra por un túnel
  SSH (`ssh -L 8787:127.0.0.1:8787 servidor`) o detrás de un proxy con HTTPS.
- Detrás de un proxy (Caddy, nginx): `DOCKER_ALLOWED_HOSTS=tu.dominio` y que el
  proxy conserve `Host` (`proxy_set_header Host $host;` en nginx; Caddy lo hace
  solo). La app rechaza peticiones con otro nombre y las que vienen de otras webs
  (ver [SECURITY.md](SECURITY.md)).
- Sin GPU, deja `OLLAMA_URL=` vacío (textos con la primera frase) o apúntalo a
  un equipo con Ollama. Escribir los textos en el Mac antes de mover la base de
  datos también funciona: quedan guardados en la biblioteca.
- `/api/health` es el chequeo de salud (el de la imagen ya lo usa).

---

## CI

`.github/workflows/ci.yml`, en cada push a `main` y `dev` y en cada pull request:

- **Tests** con Python 3.11 y 3.13: `ruff check` y `pytest` (con ffmpeg, contra
  el Zernio falso: nada sale a internet).
- **Imagen Docker**: los tests dentro de la imagen y una prueba de humo de la app
  (arranca y responde en `/api/health`).

En local, lo mismo: `make check` y `make docker-test`.
