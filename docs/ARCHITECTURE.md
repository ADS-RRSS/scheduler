# Arquitectura

Para quien vaya a tocar el código. El recorrido funcional está en
[FLOW.md](FLOW.md); esto es el mapa de los archivos y de por qué están así.

---

## Principios

**1. La lógica de horarios es matemática pura.** `app/scheduler/planner.py` no
importa nada de la base de datos ni de la red. Recibe una política y unas fechas,
y devuelve fechas. Por eso se puede probar de forma exhaustiva —incluidos los
cambios de hora— sin levantar nada, y es la parte que más tests tiene.

**2. Los errores se clasifican, no se tragan.** `app/services/zernio.py`
separa "temporal", "definitivo" y "problema de clave". De esa distinción sale
todo el comportamiento de reintentos, que es lo que evita machacar la API o
perder vídeos.

**3. Un único camino de publicación.** Desde la versión 2 sólo existe Zernio.
Menos código, menos ramas, menos documentación que mentir.

**3 bis. Una cuenta, varias colas.** Una cuenta de TikTok se conecta una sola
vez; lo que se multiplica son sus colas (`Schedule`), cada una con sus vídeos y
sus horas. Duplicar la cuenta habría duplicado también la clave, el contador
diario y el «un vídeo cada vez», que son propiedades de la cuenta, no de la cola.

**4. Sin paso de compilación.** La interfaz es HTML + CSS + módulos ES servidos
tal cual. Se instala con `pip install` y funciona. Nada de node, bundlers ni
lock-files que se pudren.

**5. Un único `/api/state`.** El frontend pide un objeto con todo y repinta la
página activa. Sin gestión de estado, sin caché que invalidar. Las páginas de
administración (sistema, base de datos, actividad filtrada) y el calendario piden
lo suyo bajo demanda. «Todo» es todo lo que está por salir; del historial, lo
reciente (ver abajo). Como «todo» llega a varios MB (cada vídeo entregado a
Zernio sigue en la lista hasta publicarse), no se pide a ciegas: cada 5 s
mientras alguien usa la página (ratón o teclado en el último minuto) o hay un
vídeo subiéndose o procesándose, cada 30 s en reposo y nunca con la pestaña en
segundo plano; al volver se pide al momento (`poll` en `app.js`).

**5 bis. Un repintado nunca rehace lo que estás tocando.** Cada pocos segundos se
repinta la página, así que lo que tenga campos se protege: un formulario sólo se oculta en
la transición que le toca (el de conectar cuentas se cerraba en cada refresco),
las tarjetas de Cuentas no se redibujan mientras un campo tiene el foco o tiene
cambios sin guardar (`editingInside`, «Descartar»), Programación espera a que no
haya cambios (`formDirty`) y un `<select>` sólo se rehace si cambian sus opciones
y nunca abierto (`fillSelect`). Lo que se dibuja a partir de datos se compara con
lo último dibujado (una firma) y no se toca si no cambió.

---

## Estructura

```
app/
├── main.py              Arranque, lifespan, rutas raíz, login
├── config.py            Settings desde .env (pydantic-settings)
├── models.py            Tablas SQLAlchemy (Schedule = una "cola",
│                        VideoMetric = sus números, Comment = su buzón)
├── dbtypes.py           UTCDateTime: arregla el manejo de zonas de SQLite
├── db.py                Motor, sesiones, PRAGMAs y migraciones
├── crypto.py            Cifrado Fernet de las API keys
├── security.py          Código de acceso opcional
├── schemas.py           Validación de entrada (pydantic)
│
├── scheduler/
│   ├── planner.py       ★ Matemática pura del "cuándo"
│   ├── worker.py        Bucle de fondo (asyncio): publicar, sondear, extras
│   └── captioner.py     Segundo bucle: escribe los textos, de uno en uno
│
├── services/
│   ├── zernio.py        ★ Cliente de Zernio: presign, subida, post, estado
│   ├── accounts.py      Cuentas, claves cifradas, verificación
│   ├── library.py       La biblioteca: un archivo por vídeo, textos por plataforma
│   ├── textcheck.py     Revisión y arreglos de un texto escrito a mano (el editor)
│   ├── backups.py       Copias de la base de datos: a mano, antes de borrar, diarias
│   ├── instance.py      Una sola copia de la app por base de datos (candado)
│   ├── calendar.py      La semana: lo publicado, lo de Zernio y lo previsto por las reglas
│   ├── publisher.py     ★ Orquesta una publicación completa (ahora, o con hora)
│   ├── delegation.py    Entregar la cola entera a Zernio: plan, envío, rescate
│   ├── queue.py         Vídeos de cada cola, carpeta vigilada, contador diario
│   ├── media.py         ffprobe y validación previa
│   ├── captions.py      Variantes, campos y hashtags (plantillas)
│   ├── autocaption.py   Whisper → (Ollama | heurística) → texto del vídeo
│   ├── hashtags.py      Normalizar, banco de temas, ranking por evidencia
│   ├── analytics.py     Métricas por vídeo y agregados por hora / día / hashtag
│   ├── comments.py      Buzón de comentarios: lectura, respuesta, ocultar
│   ├── scheduling.py    Puente modelos ↔ planner, y alta/baja de colas
│   ├── events.py        Registro de actividad (búsqueda, recorte)
│   └── notify.py        Avisos: Discord · Slack · ntfy · Telegram · genérico
│
├── api/
│   ├── routes_api.py    Estado, cuentas, programación, cola, analíticas,
│   │                    comentarios, actividad
│   ├── routes_library.py La biblioteca: subir, listar (con su revisión), editar, en bloque
│   ├── routes_text.py   El editor: revisar, arreglar, sugerir hashtags, proponer con IA
│   ├── routes_calendar.py  GET /api/calendar
│   ├── routes_admin.py  Sistema, copias, mantenimiento, borrar todo
│   ├── routes_db.py     Navegador de base de datos y consola SQL
│   └── serializers.py   Modelo → JSON
│
└── web/
    ├── templates/index.html
    └── static/  app.js (router y páginas) · library.js · composer.js (el editor de textos)
                 · calendar.js · admin.js (cuentas, ajustes, BD) · shared.js · app.css
                 · labels.js (etiquetas y selector de carpetas) · datetimepicker.js (día y hora)
tools/
├── mock_zernio.py       Zernio (y Ollama) falso + receptor de avisos, para tests y demo
├── demo.sh              Arranca la app contra el Zernio falso, con sus datos en data/demo
├── doctor.py            make doctor: diagnóstico sólo con la biblioteca estándar
├── snapshot.py          make snapshot / restore: base de datos + clave + .env, a otro Mac
└── library_copy.py · library_finalize.py   El paso de media/inbox… a la biblioteca
Makefile · Brewfile · run.sh    Instalar y arrancar (docs/INSTALL.md)
Dockerfile · docker-compose.yml Opcional: imagen fijada, demo aislada, VPS
```

---

## Decisiones que conviene conocer

### SQLite con WAL

El planificador escribe mientras la interfaz lee. Sin WAL, cada consulta de la UI
podría chocar con el worker. `PRAGMA journal_mode=WAL` lo resuelve, y para un
proceso único con escrituras esporádicas va sobrado.

### Un proceso por base de datos

Dos copias de la app sobre el mismo `scheduler.db` publicarían cada vídeo dos
veces, y con una instalación nativa y una de Docker compartiendo `data/` basta
con olvidar una terminal. `services/instance.py` toma un **candado** al arrancar
—una fila en `settings` y el mismo contenido en `data/.scheduler-instance.json`—
y lo renueva cada 30 s desde una tarea propia (no desde el tick, que puede pasar
minutos dentro de una subida). Otra copia se niega a arrancar mientras siga vivo.

El archivo existe porque el Mac y la máquina virtual de Docker leen el mismo
SQLite desde dos núcleos distintos, y el índice en memoria compartida del WAL
(`-shm`) no se comparte entre ellos: uno puede leer filas atrasadas del otro,
el candado incluido. Un archivo sustituido con `rename` lo ven los dos. Además,
`main.ensure_port_free()` comprueba el puerto **antes** de arrancar el
planificador: uvicorn abre el puerto después del arranque, y una segunda copia
llegaba a publicar un momento antes de fallar.

### `/api/state` no recorta lo que está por salir

`queue.for_state()` lista **todos** los vídeos vivos de cada cuenta (en cola,
subiendo, en Zernio) y, del historial (publicados y fallidos), los
`STATE_HISTORY_LIMIT` más recientes. Los contadores salen de un `COUNT … GROUP
BY` (`queue.status_counts`), no de la lista. Antes se cargaban los 500 más
recientes de todo: con más de 500 filas, los vídeos más antiguos —los próximos
en salir— desaparecían de la cola y los números se calculaban sobre lo que
quedaba. Las métricas y la carpeta de cada vídeo vienen con `selectinload` y los
comentarios en una consulta agrupada: el coste ya no crece con cada vídeo (antes,
dos consultas por vídeo cada 5 s). Las transcripciones no viajan en el estado
(sólo un extracto); el editor pide el vídeo entero a `GET /api/videos/{id}`.

### El calendario simula con el mismo planificador

`services/calendar.py` no tiene reglas propias. Lo real sale de las filas
(`published_at` o `scheduled_at`) y lo **previsto** de `scheduling.preview()` para
cada cola activa con vídeos esperando, en el orden de salida (`queue.pending`).
Para que dos colas de una cuenta no se pisen en la simulación, `preview()` acepta
`booked`: los instantes que ya se dieron a las colas anteriores de la cuenta,
que cuentan contra su máximo diario y su separación mínima como si estuvieran
reservados. Es el mismo orden en el que `replan_account()` planifica de verdad.
Se simulan como mucho 500 vídeos por cola (una semana nunca los tiene); 4 cuentas
con 300 esperando cada una cuestan ~125 ms, por eso el frontend lo pide al entrar
y cada minuto, no en cada refresco.

### El editor revisa con las reglas del generador

`services/textcheck.py` aplica a un texto escrito a mano las comprobaciones que
`autocaption` aplica a los borradores del modelo (reutiliza sus expresiones) y
monta la vista previa con las mismas funciones que el publicador
(`captions.render_caption`, `hashtags.with_fixed`, `hashtags.plain_tags`), así que
lo que enseña el editor es lo que sale. No toca la base de datos: la ruta le pasa
el contexto (fijos de Ajustes, lo que dice el vídeo, las otras descripciones de la
serie). El frontend normaliza los hashtags igual que `hashtags.normalize`
(`composer.js`, `normalizeTag`), probado con los mismos casos.

### Una petición de otra web no llega a ninguna ruta

`security.request_guard` corre antes que el código de acceso: rechaza las
peticiones que cambian algo si el navegador dice que vienen de otra página
(`Sec-Fetch-Site`, `Origin`) y cualquiera dirigida a un nombre que no sea de la
app (`Host`, contra el DNS rebinding). Ver [SECURITY.md](SECURITY.md).

### Migraciones sin Alembic

`db.migrate()` corre al arrancar. Dos mecanismos:

- `add_missing_columns()` compara los modelos con `PRAGMA table_info` y añade
  con `ALTER TABLE … ADD COLUMN` las columnas nuevas (nullable o con default).
  Cubre el 95 % de los cambios.
- Migraciones numeradas para lo estructural. La **v2** consolidó las cuentas en
  Zernio: movió los vídeos de las cuentas locales y OAuth a la primera cuenta de
  Zernio (descartando los duplicados por checksum), renombró columnas
  (`open_id → external_id`, `relay_account_id → zernio_account_id`,
  `relay_api_key_enc → api_key_enc`, `publish_id → zernio_post_id`), eliminó las
  de OAuth y del modo manual, y borró `oauth_states`. Requiere SQLite ≥ 3.35.
  La **v3** abrió la puerta a varias colas por cuenta: quitó el `UNIQUE` del
  índice `schedules.account_id`, y ató cada vídeo a su cola con
  `video_items.schedule_id` (hasta entonces había exactamente una por cuenta, así
  que el reparto es inequívoco).

Las numeradas que *rellenan* columnas nuevas corren después de
`add_missing_columns()`; las que renombran o borran, antes.

La versión del esquema vive en `settings` (`schema_version`). Una base de datos
sin versión pero con `open_id` es v1; una sin `open_id` es nueva.

### `UTCDateTime` en vez de `DateTime(timezone=True)`

SQLite no tiene tipo fecha con zona: SQLAlchemy devuelve datetimes *naive*, que
luego explotan al compararlos con los que sí llevan zona. `app/dbtypes.py` fuerza
UTC consciente en ambos sentidos.

### `autoflush=True`

El planificador modifica objetos y a continuación consulta la cola dentro de la
misma transacción. Con `autoflush=False` esas consultas leen filas viejas.

### El hueco se consume aunque falle

Si un fallo no gastara el hueco, el siguiente vídeo saldría inmediatamente después
del error. La excepción es «Ahora»: un forzado que falla no quema el hueco
programado, y uno que triunfa reinicia la cadencia al confirmarse.

### Sólo una publicación por ciclo y cuenta

Una subida de 500 MB dentro del ciclo retrasa todo lo demás. Limitando a una, se
acota cuánto puede bloquear. Las consultas de estado van primero. El límite es
**por cuenta**, no por cola: de todas las colas con el hueco vencido publica la
del hueco más antiguo (el orden de la lista sólo desempata) y no vuelve a esa
cuenta hasta el siguiente ciclo. Por antigüedad y no por posición, porque si no
una primera cola muy cargada dejaría a las demás sin publicar nunca.

### Los límites de la cuenta se aplican en `scheduling`, no en el planner

`planner.py` sigue sin saber qué es una cuenta. Recibe dos cosas más: un
`DayLimit` con el contador de toda la cuenta, y un `Spacing` con los instantes
que la cuenta ya tiene ocupados (publicados, programados o entregados a Zernio)
más la distancia que hay que guardar con cada uno.

`Spacing` es la pieza que permite que **las colas funcionen a la vez**. La
separación mínima es una distancia a los vecinos, no un turno de espera: un
candidato que cae demasiado cerca de algo reservado salta justo detrás de ello y
se vuelve a comprobar desde el principio, así que el salto nunca acaba fuera de
una franja ni en un día lleno. Una cola nueva coloca su primer vídeo hoy, en los
huecos que dejó la otra, aunque la otra tenga dos semanas reservadas.

Antes esto se hacía adelantando el «ahora» hasta la **última** hora reservada de
la cuenta, que es justo lo contrario: bastaba con delegar una cola en Zernio para
que la segunda no encontrara hueco hasta el final de la primera.

Hay una sola separación, la de la cuenta (`Account.min_gap_minutes`), y hace los
dos trabajos: es el suelo del intervalo aleatorio dentro de cada cola y la
distancia entre colas. Las colas tenían la suya hasta la v3.3; con dos números
llamados igual, el de la cola podía quedar por debajo del de la cuenta.

### «Empezar a publicar» es un «ahora» más tarde

`Schedule.start_date` + `start_time` (día y hora locales, en la zona de la
cola; sin hora, las 00:00) no llegan al planner como regla nueva:
`scheduling.earliest()` adelanta el `now` que se le pasa hasta ese momento, y el planner hace lo de siempre desde ahí (
franjas,
días, topes, separación). Como `plan_next_slot` y `preview` son los dos únicos
que llaman al planner, lo respetan a la vez la hora fijada, la vista previa, el
Calendario y la delegación. `plan_next_slot` vacía los dos campos cuando ese momento llega.

### Tras publicar se replanifican todas las colas de la cuenta

Lo que reserva una cola mueve a las demás: consume presupuesto diario y ocupa
una hora de la que hay que mantenerse a distancia. `scheduling.replan_account()`
las recalcula todas en el orden en que están en la lista, de modo que cada una
esquiva lo que acaban de fijar las anteriores y la primera se queda la hora
disputada. Un reintento nunca se adelanta: ya lleva su propia hora.

### Un vídeo siempre pertenece a una cola

`video_items.schedule_id` es `ON DELETE SET NULL` para que borrar una fila de
`schedules` a mano (el navegador de base de datos lo permite) no se lleve los
vídeos por delante. Al eliminar una cola desde la interfaz hay que
elegir explícitamente entre mover sus vídeos a otra o borrarlos con ella
(`?move_to=` o `?purge=`); sin ninguna de las dos, `delete_schedule()` se niega,
porque perder un año de historial por un clic sería el peor de los defectos
posibles. Si aun así algún vídeo se queda huérfano, el worker lo devuelve a la
primera cola de su cuenta y lo anota en Actividad: si no, sería invisible en la
interfaz y no publicaría nunca.

El registro de actividad nunca se borra con la cola. `EventLog` no tiene claves
foráneas a propósito: es un diario plano, y sus `video_id`/`account_id` son
referencias flojas que sobreviven a lo que ya no existe.

### Jitter sólo hacia delante

Un jitter negativo podría colocar el hueco en el pasado o incumplir el intervalo
mínimo recién aplicado.

### Un archivo, una fila de biblioteca, una fila por cola

`services/library.py`: cada vídeo es una fila de `library_items` (única por
checksum) y un archivo en `media/library/<serie>/`, con los textos de cada
plataforma (`tiktok_*`, `youtube_*`). Una cola lo toma con `library.enqueue()`,
que crea su `video_items` apuntando al mismo archivo (`library_item_id`) con los
textos de la plataforma de la cuenta y `caption_source="biblioteca"`: mientras
siga así, `library.propagate()` le lleva cada cambio de la biblioteca (texto
terminado, reescrito o editado), hasta que el vídeo sale hacia Zernio o se edita
en la cola. El captioner escribe primero la biblioteca (una pasada por vídeo,
hashtags elegidos por plataforma con `autocaption.retag`) y sólo escribe una fila
de cola si se le pidió a esa cola. Ningún paso mueve ni borra el archivo: se
borra desde la biblioteca y sólo si ninguna cola lo necesita. `library.scan()`
es la carpeta vigilada; deja en paz lo modificado hace menos de 5 s.

La portada no tiene columna: es el archivo de al lado (`queue.cover_path()`:
`<vídeo>.jpg|.jpeg|.png`, o con `_portada` delante de la extensión), como el
`.txt`. Al publicar, `ZernioClient.publish(thumbnail=…)` la sube con su propio
presign y la pone en `mediaItems[0].thumbnail` (YouTube) o en
`tiktokSettings.video_cover_image_url` (TikTok); si esa subida falla, el vídeo
sale sin ella. Todo post de TikTok lleva además `video_cover_timestamp_ms: 0`:
el valor por defecto de Zernio es 1000, y ContentBuilder pone la portada sólo en
los primeros 0,1 s, así que sin él la portada era una escena cualquiera.

### Sólo hay una forma de publicar

El carril de TikTok for Business por el que conecta Zernio publica los vídeos en
público y nada más. La v3 dejó de fingir lo contrario: `zernio.PRIVACY_LEVEL` es
una constante, `post_options()` no negocia nada, y los borradores, el selector de
privacidad y Sound Boost desaparecieron con la migración v4.

### El texto se escribe en otro bucle

`captioner.CaptionWorker` es una tarea asyncio aparte de `SchedulerWorker`.
Coge el vídeo más antiguo con `caption_status = pending` —salvo que alguno se
pidiera a mano (`caption_priority = 1`: «Regenerar», «Reescribir» en uno suelto),
que pasa delante de todo el lote—, lo marca `generating`
(la cola lo pinta al instante), y ejecuta `autocaption.generate()` en un hilo:
Whisper es CPU puro y bloqueante, y el bucle de publicación no debe notarlo.
Uno a la vez, porque dos Whisper simultáneos son más lentos que uno detrás de
otro. Si al guardar el vídeo ya no está en `generating` (el usuario escribió su
texto), el resultado se descarta. El publicador, por su parte, no sube un vídeo
en `pending`/`generating` hasta `caption_max_wait_minutes`; después lo marca
`failed` y lo publica con lo que tenga.

`autocaption.py` y `hashtags.py` no tocan la base de datos: reciben un
`Context` (fijos, banco, puntuaciones aprendidas) y devuelven una
`Composition`. Eso es lo que hace trivial probarlos y sustituir Whisper por un
transcriptor falso en los tests.

### Analíticas y comentarios van por libre

Los dos son extras de sólo lectura sobre la misma API key. Cuelgan del tick del
worker pero con su propio reloj (`_refresh_analytics`, `_refresh_comments`), se
tragan sus errores y no pueden retrasar ni romper una publicación. Cada uno
guarda su copia local: `video_metrics` (una fila por vídeo, sobrescrita) y
`comments` (una fila por comentario, única por vídeo + id externo).

Los comentarios se **consultan**, no se reciben: el webhook `comment.received`
de Zernio necesitaría una URL pública y esto corre en `127.0.0.1`. Como Zernio
cachea la lista 10 minutos, preguntar más de una vez cada media hora no aporta
nada.

### La subida presignada va con `Content-Length`

Los presignados tipo S3/R2 rechazan *chunked transfer encoding*. httpx sólo usa
un cuerpo de longitud fija si le das la cabecera explícita; por eso
`ZernioClient.upload()` la pone aunque el cuerpo sea un iterador.

### La URL de TikTok llega tarde

Zernio devuelve `published` de inmediato pero TikTok expone el id del vídeo
minutos después. El publicador deja el vídeo en `PROCESSING` y lo sondea; pasado
`ZERNIO_URL_GRACE_MINUTES` lo da por publicado aunque no tenga URL.

### El navegador de base de datos usa reflexión y SQL crudo

`routes_db.py` no conoce los modelos: refleja las tablas que existan, valida
cada identificador contra esa reflexión antes de interpolarlo, y lee los valores
como los guarda SQLite (JSON y fechas salen como texto). Las columnas `*_enc` se
enmascaran y no se editan. La consola SQL corre en una conexión `sqlite3` aparte
con `PRAGMA query_only=1`, además del filtro por prefijo.

### `exclude_video_id` en el contador diario

Al calcular el hueco de un vídeo, ese mismo vídeo no debe contar contra el máximo
del día. Si contase, cada recálculo lo empujaría un día hacia delante.

### Los avisos salen de la URL guardada en Ajustes

`notify.resolve_url()` prefiere el valor de la base de datos y cae al `.env`
sólo si está vacío. El formato del envío se adapta al servicio por la URL.

---

## Tests

```
tests/
├── test_planner.py          48  Horarios: franjas, medianoche, topes, cambios de hora
├── test_autocaption.py      31  Whisper (falso) → Ollama (falso) → texto y hashtags
├── test_queues.py           27  Varias colas por cuenta: reparto, límites, borrado
├── test_library.py          24  Biblioteca: carpetas, textos por plataforma, colas
├── test_delegation.py       24  La cola entera a Zernio: plan, entrega, rescate
├── test_textcheck.py        22  La revisión del editor, sus arreglos y sus rutas
├── test_zernio.py           21  Conexión y publicación contra el Zernio simulado
├── test_captions.py         19  Variantes, campos, hashtags, recortes
├── test_media.py            18  Validación previa sobre vídeos reales de ffmpeg
├── test_admin.py            16  Cuentas, actividad, copias, mantenimiento, navegador de BD
├── test_queue_counting.py   15  Contador diario: zonas horarias, cambios de hora, colas
├── test_reelinfo.py         13  La ficha .txt de ContentBuilder (formato 1)
├── test_sheet_v2.py         11  La ficha con los textos de cada plataforma y el interruptor de IA
├── test_shuffle.py          10  Mezclar y el orden de salida
├── test_migration.py        10  Columnas nuevas y migraciones con datos
├── test_start_date.py        9  «Empezar a publicar»: hora fijada, vista previa, calendario, Zernio, hora de inicio
├── test_guard.py             8  Otras webs, nombres, y una sola copia por base de datos
├── test_notify_formats.py    8  Formatos por servicio y URL de Ajustes
├── test_wipe.py              7  Borrar todo, con sus tres comprobaciones
├── test_comments.py          7  Buzón de comentarios
├── test_analytics.py         6  Métricas y agregados
├── test_aigc.py              6  La etiqueta de IA por vídeo
├── test_backups.py           4  Copias diarias y su rotación
├── test_calendar.py          4  La semana: marcas reales, previstos y dos colas sin pisarse
├── test_library_texts.py     4  Revisión en la lista y hashtags en bloque
├── test_upload_fallback.py   4  La ruta alternativa cuando R2 no responde
├── test_notify.py            4  Los avisos nunca rompen una publicación
├── test_snapshot.py          3  Instantánea a otro Mac y vuelta
├── test_caption_priority.py  3  Lo pedido a mano pasa delante; la estimación
├── test_state.py             2  /api/state con cientos de vídeos: nada pendiente se pierde
└── test_doctor.py            1  make doctor corre sin nada preparado
                            ───
                            369
```

`make check` (ruff + pytest) es lo que corre CI en Python 3.11 y 3.13, y
`make docker-test` lo mismo dentro de la imagen.

`tools/mock_zernio.py` no es un doble complaciente: exige el `Bearer` correcto,
un `Content-Length` explícito en la subida presignada, que el archivo esté
subido antes de crear el post y que los dos indicadores de consentimiento vayan
a `true`; devuelve la URL de TikTok con retraso, como el real, y permite forzar
`400/401/402/429/500` y rechazos posteriores (`/_mock/config`).

---

## Añadir cosas

**Un preset de intervalo** → `INTERVAL_PRESETS` en `planner.py`. La interfaz se
actualiza sola vía `/api/presets`.

**Un ajuste nuevo por cola** → columna en `Schedule`, campo en `ScheduleIn`, línea
en `schedule_json()` y control en la sección que toque de **Programación**.
`add_missing_columns()` se encarga de las bases de datos que ya existan. Si el
ajuste es de la cuenta entera y no de una cola, va en `Account` + `AccountPatch`.

**Un preset de franja horaria** → `WINDOW_PRESETS`, mismo archivo.

**Una categoría de hashtags** → una entrada en `data/hashtags.json`; nada más.

**Otro motor de texto** (otro LLM, una API) → una función que reciba
`Transcript` y devuelva `{caption, hashtags}` en `autocaption.compose()`, junto
a `compose_with_ollama()`.

**Una métrica nueva** → campo en `PostMetrics` (`zernio.py`) y en
`ZernioClient.parse_analytics`, columna en `VideoMetric`, línea en
`serializers.metric_json` y, si va en los totales, en `analytics.summary()`.

**Un canal de avisos** → una rama en `notify.prepare()` que detecte la URL y
devuelva el `Outgoing` adecuado.

**Un campo nuevo en la configuración** → columna en `models.Schedule` (con
default: la migración la añadirá sola), campo en `schemas.ScheduleIn`, salida en
`serializers.schedule_json`, control en `index.html` y lectura en
`saveSchedule()` de `app.js`.

**Un cambio estructural** (renombrar o borrar columnas) → una migración numerada
en `db.py`, subir `SCHEMA_VERSION`, y un test en `test_migration.py` que
construya la forma antigua y compruebe la nueva.
