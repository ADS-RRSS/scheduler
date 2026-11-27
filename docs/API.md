# La API local (`http://127.0.0.1:8787`)

Todo lo que hace la interfaz pasa por esta API, y es también la puerta para los demás proyectos de `_RRSS`
(Tunnel, Community Farm, el arnés `rrss`): **nadie escribe en `data/scheduler.db` a mano**, porque la API recalcula
horas y deja rastro en la Actividad. Sólo escucha en local; con `UI_PASSCODE` pide además la cookie del login
(ver [SECURITY.md](SECURITY.md)). El esquema exacto de cada petición está en `app/schemas.py` y el código, en
`app/api/routes_*.py` (y `app/web/` para las páginas).

Las rutas que usan otros proyectos van marcadas con ⇄.

## Estado y ajustes (`routes_api.py`)

| Método y ruta | Qué hace |
|---|---|
| `GET /api/health` | Si el servidor está vivo |
| `GET /api/state` ⇄ | Cuentas, colas y vídeos de cada una, de un tirón (lo que pinta la portada) |
| `GET /api/presets` | Los intervalos, franjas y días que ofrece el editor de colas |
| `POST /api/system/pause` | Pausa o reanuda la programación (`{"paused": true}`); queda en la Actividad |
| `GET /api/settings` · `PUT /api/settings` | Ajustes generales (hashtags comunes, webhook…) |
| `POST /api/settings/webhook-test` | Manda un aviso de prueba al webhook |
| `GET /api/events` | La Actividad (lo que ha pasado, con filtros) |

## Cuentas y colas (`routes_api.py`)

| Método y ruta | Qué hace |
|---|---|
| `POST /api/zernio/accounts` | Valida una clave de Zernio y lista las cuentas que puede publicar |
| `POST /api/accounts` | Conecta una o varias cuentas con la misma clave |
| `PATCH /api/accounts/{id}` | Nombre, activa o no, máximo al día y separación mínima de la cuenta, y la categoría de YouTube (`youtube_category_id`) |
| `GET /api/accounts/{id}/key` · `PUT …/key` | Enseña la clave (queda en la Actividad) · la sustituye tras validarla |
| `POST /api/accounts/{id}/test` | Prueba la conexión con Zernio |
| `DELETE /api/accounts/{id}` | Quita la cuenta |
| `POST /api/accounts/{id}/schedules` ⇄ | Añade una cola, copiando si se quiere las reglas de otra |
| `PUT /api/accounts/{id}/schedules/order` | El orden de las colas de la cuenta |
| `PUT /api/schedules/{id}` | Guarda la configuración entera de una cola |
| `PATCH /api/schedules/{id}` ⇄ | Renombra, enciende/apaga u oculta una cola |
| `DELETE /api/schedules/{id}` | Quita la cola |
| `GET /api/schedules/{id}/preview` | Las próximas horas que daría la cola |
| `POST /api/schedules/{id}/shuffle` · `…/unshuffle` | Baraja lo que espera · vuelve al orden de llegada |
| `POST /api/schedules/{id}/reorder` | Mueve vídeos a mano: a un turno, o antes o después de otro |
| `GET /api/schedules/{id}/delegation` | La hora exacta que tendría cada vídeo si se delegara en Zernio |
| `POST /api/schedules/{id}/delegate` ⇄ · `…/delegation/cancel` | Delega la cola en Zernio (la app puede cerrarse) · la recupera |
| `POST /api/schedules/{id}/videos` ⇄ | Añade vídeos de la biblioteca a la cola, con los textos ya escritos para su red |
| `POST /api/schedules/{id}/videos/remove` ⇄ | Saca varios vídeos que esperan |

## Vídeos (`routes_api.py`)

| Método y ruta | Qué hace |
|---|---|
| `GET /api/videos/{id}` ⇄ | Un vídeo con todo (transcripción incluida) |
| `PATCH /api/videos/{id}` ⇄ | Título, texto, primer comentario, prioridad o hashtags de un vídeo; con `schedule_id`, lo pasa a otra cola (así mueve vídeos el arnés) |
| `DELETE /api/videos/{id}` | Lo quita de la cola |
| `POST /api/videos/{id}/publish-now` ⇄ | Lo publica ya |
| `POST /api/videos/{id}/retry` | Reintenta uno fallido |
| `POST /api/videos/{id}/undelegate` ⇄ | Recupera de Zernio uno delegado, sin tocar el resto |
| `POST /api/videos/{id}/caption/regenerate` | Lo vuelve a escuchar y escribe texto y hashtags nuevos |
| `GET /api/videos/{id}/file` | El vídeo, para la vista previa |

## Analíticas, comentarios y calendario

| Método y ruta | Qué hace |
|---|---|
| `GET /api/analytics` · `POST /api/analytics/refresh` | Totales, mejores vídeos y franjas · los pide a Zernio ya ([ANALYTICS.md](ANALYTICS.md)) |
| `GET /api/comments` | Los comentarios, del más nuevo al más viejo (`mine=false` quita los tuyos) |
| `POST /api/comments/refresh` | Los recoge ya de una cuenta o de todas |
| `POST /api/comments/{id}/reply` ⇄ · `…/like` ⇄ | Responde · da «me gusta» (sólo TikTok) |
| `POST /api/comments/{id}/hide` · `…/answered` | Oculta o enseña · marca respondido a mano |
| `POST /api/videos/{id}/comment` ⇄ | Un comentario propio en un vídeo publicado (Community Farm) |
| `GET /api/calendar` | `days` días desde `start` (por defecto, el lunes de esta semana) — `routes_calendar.py` |

Más sobre comentarios en [COMMENTS.md](COMMENTS.md).

## Biblioteca (`routes_library.py`)

| Método y ruta | Qué hace |
|---|---|
| `GET /api/library` | Las carpetas con sus recuentos y los totales |
| `GET /api/library/items` | Los vídeos de una carpeta (o los que casan con `q`), con la cola en que está cada uno |
| `GET /api/library/{id}` · `…/file` · `…/cover` | Un vídeo · el fichero · la portada |
| `POST /api/library/upload` ⇄ | Sube vídeos con su `.txt` y su portada |
| `POST /api/library/scan` | Registra lo copiado a mano en `media/library` |
| `POST /api/library/folders/rename` · `POST /api/library/move` | Renombra una carpeta · mueve vídeos de carpeta |
| `POST /api/library/folders/labels` · `…/labels/color` | Etiquetas de una carpeta · el color de una etiqueta |
| `PATCH /api/library/{id}` · `POST …/{id}/rewrite` | Edita los textos · los reescribe tras volver a escucharlo |
| `POST /api/library/bulk` · `POST /api/library/tags` | «Reescribir», «Textos de la ficha» o «Borrar» en lote · añade o quita hashtags en lote |
| `POST /api/library/ai-texts` | El interruptor «Reescribir los textos con IA» |
| `DELETE /api/library/{id}` | Borra el vídeo de la biblioteca |

## Textos (`routes_text.py`)

| Método y ruta | Qué hace |
|---|---|
| `POST /api/text/check` | Qué falla en unos textos y cómo se leerá la publicación ([CAPTIONS.md](CAPTIONS.md)) |
| `POST /api/text/fix` | Los mismos textos con los arreglos que no necesitan modelo (no guarda) |
| `GET /api/text/hashtags` | Hashtags que proponer en el editor |
| `POST /api/text/suggest` | «✨ Proponer con IA»: Ollama los reescribe (no guarda) |

## YouTube (`routes_youtube.py`)

| Método y ruta | Qué hace |
|---|---|
| `GET /api/youtube/categories` | Las categorías de YouTube (id y nombre) |
| `GET /api/youtube/{cuenta}/videos` ⇄ | Los vídeos publicados del canal desde `since` |
| `POST /api/youtube/{cuenta}/videos/{id}/metadata` ⇄ | Cambia categoría, idioma, lista o miniatura de uno publicado (`dry_run` contesta sin mandar nada); lo usa el optimizador de Tunnel |

## Administración (`routes_admin.py`, `routes_db.py`)

Ver [ADMIN.md](ADMIN.md): estas rutas enseñan secretos, editan filas o borran datos.

| Método y ruta | Qué hace |
|---|---|
| `GET /api/admin/system` · `GET /api/admin/disk` | Estado del servidor y del worker · el disco |
| `GET/POST /api/admin/backups` · `GET/DELETE …/backups/{nombre}` | Copias de la base: listar, hacer, bajar, borrar |
| `POST /api/admin/events/prune` | Recorta la Actividad antigua |
| `POST /api/admin/videos/clear-failed` | Quita los vídeos fallidos |
| `GET /api/admin/videos/missing` · `POST …/missing/clean` | «Sin archivo en disco»: qué se quitaría · quitarlo (con copia antes) |
| `GET /api/admin/wipe/preview` · `POST …/wipe/challenge` · `POST …/wipe/answer` · `POST /api/admin/wipe` | «Borrar todo», con sus tres confirmaciones |
| `GET /api/db/tables` · `GET …/tables/{tabla}` · `GET …/{tabla}/export.csv` | El navegador de la base: tablas, filas, CSV |
| `GET/PUT/DELETE /api/db/tables/{tabla}/rows/{pk}` | Leer, editar o borrar una fila |
| `POST /api/db/query` | Una consulta SQL de sólo lectura (conexión aparte, sólo lectura) |

## Páginas (`app/web/`)

`GET /` (la interfaz), `GET/POST /login` (sólo con `UI_PASSCODE`) y `GET /favicon.ico`.
