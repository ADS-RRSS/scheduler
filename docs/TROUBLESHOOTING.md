# Cuando algo falla

Empieza siempre por **`make doctor`**: en medio minuto comprueba Python, el
entorno, ffmpeg, la base de datos, la clave de cifrado, el disco, Whisper,
Ollama, la red hasta Zernio y la ruta de subida, y dice cómo arreglar lo que
falle. Funciona aunque el entorno virtual esté roto.

Después, **Seguimiento → Actividad**: cuenta, paso a paso, lo que ha hecho la
herramienta y por qué. **Ajustes → Sistema** dice en qué estado está todo, y
<http://127.0.0.1:8787/api/health> lo mismo en JSON.

---

## La app no arranca

### `ModuleNotFoundError: No module named 'fastapi'`

El entorno virtual no está o no se instalaron las dependencias:

```bash
make setup
```

Ejecuta siempre con `make run` (o `./run.sh`), no con `python3` a secas.

### «Hace falta Python 3.11 o superior»

El `python3` de un Mac recién estrenado es el 3.9 de Xcode. `brew install
python@3.13` (o `make bootstrap`) y vuelve a lanzar: `run.sh` rehace el entorno
virtual con el nuevo. Lo mismo si el entorno quedó roto tras un `brew upgrade`
que se llevó el Python con el que se creó.

### «Otra copia de Scheduler está usando esta base de datos»

Hay otra copia abierta sobre el mismo `scheduler.db` —en otra terminal, en
Docker (`make docker-down`) o en otro puerto— y dice cuál (equipo, proceso,
puerto). Ciérrala: dos copias publicarían cada vídeo dos veces. Si sabes que ya
no existe (se cortó la luz, se mató el contenedor), la marca caduca a los 90 s;
o arranca una vez con `INSTANCE_TAKEOVER=true`.

### `El puerto 8787 ya está en uso` / `Address already in use`

La app ya está abierta (en otra terminal o en Docker) u otro programa usa ese
puerto. La app se niega **antes** de arrancar nada, para no publicar dos veces:

```bash
lsof -nP -iTCP:8787 -sTCP:LISTEN     # quién es
make docker-down                     # si es el contenedor
```

O cambia `PORT` en el `.env` si es otro programa.

### `Host no permitido` / `Petición bloqueada: viene de otra página web`

La protección contra otras webs (ver [SECURITY.md](SECURITY.md)). Si abres la app
con un nombre que no es `127.0.0.1` ni `localhost` (un dominio, la IP de la red),
añádelo a `ALLOWED_HOSTS` en `.env`. Detrás de un proxy, que conserve la
cabecera `Host`.

### `CERTIFICATE_VERIFY_FAILED` en un script

El Python del instalador de python.org viene sin certificados raíz hasta
ejecutar «Install Certificates.command» (en `/Applications/Python 3.x/`). La app
y `make doctor` no se ven afectados (usan `certifi`).

### `ZoneInfoNotFoundError: Europe/Madrid`

Faltan los datos de zonas horarias del sistema:

```bash
./.venv/bin/pip install tzdata
```

### Acabo de actualizar y la terminal dice `database migrated`

Es lo esperado: la base de datos se adapta sola al arrancar. En la versión 2, las
cuentas antiguas (modo manual, API de TikTok) desaparecen y sus vídeos pasan a la
primera cuenta de Zernio, sin duplicados. En la 2.1 cada cuenta pasa a tener
colas: la programación que ya tenías se queda como «Cola principal» con todos sus
vídeos dentro. Si algo no cuadra, hay una copia de todo el proyecto anterior en
`data/backups/pre-cleanup-*.tar.gz`.

### `near "DROP": syntax error` o `no such column` al migrar

SQLite demasiado antiguo (hace falta 3.35 o superior). Comprueba con
`./.venv/bin/python -c "import sqlite3; print(sqlite3.sqlite_version)"` y
actualiza Python o SQLite.

---

## Conectar con Zernio

### «Zernio rechazó la API key (401)»

Clave mal copiada o revocada. Genera una nueva en *Dashboard → API keys* y
vuelve a pegarla (o **Cuentas → Sustituir clave** si la cuenta ya existe).

### «La clave es válida pero no hay ninguna cuenta de TikTok conectada»

Conecta primero tu TikTok en el panel de Zernio (*Connect account → TikTok*) y
vuelve a pulsar **Buscar mis cuentas de TikTok**.

### «Esa cuenta de TikTok ya está conectada»

Ya existe en **Cuentas**. Si quieres cambiarle la clave, **Sustituir clave**.

### «Esa API key es válida pero no da acceso a esta cuenta de TikTok»

La clave es de otro espacio de Zernio, o la cuenta se desconectó allí. Comprueba
en el panel de Zernio a qué espacio pertenece cada cuenta.

### «No se pudo contactar con Zernio»

Sin red, o `zernio.com` caído. Prueba <https://zernio.com> en el navegador. Si
usas `ZERNIO_API_BASE` en el `.env`, comprueba que apunta a lo que crees.

---

## Los vídeos no se publican

Recorre esta lista en orden:

```
1. ¿Está encendido «Programación activa» en esa cuenta?
2. ¿La cuenta está activa (interruptor en Cuentas)?
3. ¿El punto de arriba a la derecha está verde? (ámbar = pausado)
4. ¿Hay vídeos en estado «en cola» o «programado»?
5. ¿Qué dice «Próxima publicación» en el Resumen?
6. ¿Qué cuenta **Seguimiento → Actividad**?
```

### «Próxima publicación» dice «sin hueco»

Tus reglas no permiten ninguna publicación. Causas típicas:

- Máximo diario puesto a **0**.
- Todos los días de la semana desactivados.
- Una franja horaria imposible.

Pulsa **Ver próximas publicaciones**: si sale vacío, el problema está ahí.

### El hueco pasó y no publicó nada

- **La cola está vacía**, o todo está en estado *fallido*.
- **Hay una subida en curso.** No se empieza otra hasta terminar.
- **Un vídeo tiene un reintento pendiente** con hora posterior. Míralo en
  Actividad.
- **La cuenta está desactivada.**

### Un vídeo se queda en «procesando»

Normal durante unos minutos: Zernio ya lo entregó y la herramienta espera la
URL del vídeo en TikTok (hasta 5 minutos; luego lo da por publicado). Si pasa de
60 minutos sin respuesta se marca como fallido.

### Mandé la cola a Zernio y los vídeos siguen aquí «subiendo»

La entrega marca todos los vídeos de golpe pero los sube **de uno en uno**, un
vídeo por ciclo del planificador (20 s por defecto), así que una cola larga tarda
lo que tarde la subida de cada archivo. La barra de progreso de la pestaña Cola
dice por dónde va. Causas de que no avance:

- **La programación está pausada**: las subidas pendientes esperan a que la
  reanudes (la propia página lo avisa).
- **Hay una publicación normal en curso**: una cuenta sube un vídeo cada vez.
- **Un vídeo está esperando su texto automático**: se le dan hasta
  `CAPTION_MAX_WAIT_MINUTES` y luego sale con lo que tenga. Mientras, los demás
  siguen subiendo.
- **Un error temporal de Zernio** (429, red): se reintenta con espera creciente;
  míralo en Actividad.

### Un vídeo entregado a Zernio volvió solo a la cola

Es lo que hace la herramienta cuando Zernio rechaza la entrega por algo que no se
arregla reintentando (clave revocada, límite de plan, post duplicado): en lugar
de dejarlo en el limbo, lo devuelve a la cola local con el error a la vista. El
archivo no llegó a Zernio, así que no se publicará por su cuenta. Arregla la
causa y vuelve a enviar la cola.

Si el problema era el archivo (no pasa la validación, ya no está en disco), va a
*fallido* como en una publicación normal.

### «Zernio no confirmó la publicación 48 h después de la hora programada»

Zernio retiene los posts que superan el cupo de TikTok (15 vídeos por cuenta y
24 h) y los suelta cuando la ventana deja hueco, así que un poco de retraso es
normal. Dos días no: mira ese post en el panel de Zernio; si aparece en error,
el motivo estará ahí. Puedes subir el margen con `DELEGATED_TIMEOUT_HOURS`.

Tener la app apagada no provoca este fallo: al volver, pregunta a Zernio antes
de mirar el plazo, y lo que ya salió se marca publicado con su hora. Sólo se da
por fallido un post que Zernio *sigue* teniendo pendiente pasadas las 48 h.
(Hasta el esquema v9 no era así; al arrancar, los vídeos que fallaron por esto
vuelven a «en Zernio» y se consultan una vez.)

### Vídeo fallido: «Zernio rechazó la API key (401)»

Clave revocada. **Cuentas → Sustituir clave** y luego **Reintentar** el vídeo.

### Vídeo fallido: «Zernio pide una acción en tu plan (402)»

Has superado lo que incluye tu plan (dos cuentas gratis). El mensaje trae el
enlace al panel de Zernio. **Reintentar** cuando lo resuelvas.

### Vídeo fallido: «no hay ruta hasta el servidor de subida de Zernio»

El vídeo no viaja a `zernio.com`. La API entrega una URL temporal en Cloudflare
R2 (`…r2.cloudflarestorage.com`) y los bytes van ahí, a un rango de direcciones
distinto al de la API. Por eso puede darse —y se da— el caso raro de que todo
responda y sólo la subida se quede colgada: en España los bloqueos por IP de
rangos enteros de Cloudflare van y vienen a ratos, y se llevan por delante el
destino de la subida sin tocar la API.

La aplicación se defiende sola: si las direcciones del servidor de subida no
contestan, reintenta la subida por la dirección de `media.zernio.com` —el mismo
borde de Cloudflare, que reparte por nombre de dominio y no por IP— y recuerda
durante 15 minutos la que funcionó, para que el resto de la cola no vuelva a
esperar. En la terminal se ve como `Las direcciones de … no son alcanzables
desde esta conexión; la subida ha entrado por …`. Sólo cuando tampoco hay ruta
por ahí falla el vídeo con este mensaje, y se reintenta más tarde.

Para verlo a mano:

```bash
curl -sI --max-time 10 https://zernio.com/api/v1/accounts
```

```bash
curl -sI --max-time 10 https://r2.cloudflarestorage.com/
```

Si la primera responde y la segunda se queda parada, no es tu API key ni el
vídeo: es la ruta. Pasa el ordenador a otra red (compartir los datos del móvil)
o espera, que estos bloqueos suelen durar un rato.

### «Límite de peticiones de Zernio (429)»

Demasiadas llamadas por minuto. Se reintenta solo, esperando lo que pide Zernio.
Si pasa a menudo, sube el intervalo.

### Zernio dice publicado pero no aparece la URL

TikTok tarda unos minutos en exponer el id del vídeo. La herramienta lo consulta
durante 5 minutos y luego lo da por publicado aunque no tenga la URL. Mira tu
perfil de TikTok.

### `TikTok rejected the video (…)`

Zernio lo entregó y TikTok lo rechazó. Casi siempre es el archivo: duración,
resolución o códec. Reconvierte con el comando de más abajo y **Reintentar**.

### «Estado no confirmado»

El vídeo se entregó a Zernio, pero la clave dejó de funcionar antes de poder
confirmar. Mira tu perfil de TikTok y sustituye la clave.

---

## El texto automático

### «faster-whisper no está instalado»

Actualizaste sin reinstalar dependencias. `./run.sh` lo hace solo desde la 3.1;
a mano: `./.venv/bin/python -m pip install -r requirements.txt` y reinicia.

### El primer vídeo se queda «escuchando» varios minutos

Está descargando el modelo de Whisper (460 MB para `small`; hay un aviso en
Actividad). Sólo pasa una vez. Sin internet, falla con «no se pudo descargar el
modelo»: conéctate y pulsa **Regenerar**.

### «Texto generado · sin voz: sólo hashtags» en un vídeo que sí habla

Whisper no encontró voz suficiente (menos de tres palabras). Causas típicas:
música muy por encima de la voz, o el idioma no es el de `WHISPER_LANGUAGE`.
Pon `WHISPER_LANGUAGE=` vacío para que lo detecte, o sube a `WHISPER_MODEL=medium`,
y **Regenerar**.

### El texto es raro o está en otro idioma

Con `WHISPER_LANGUAGE=es` un vídeo en inglés se transcribe mal. Vacío = detectar.
Si el texto es la transcripción literal («Hoy os cuento…») en vez de un gancho,
no hay Ollama disponible: mira Ajustes → Sistema → Textos automáticos.

### «Ollama no responde» / «el modelo no está en Ollama»

`ollama serve` tiene que estar corriendo en `OLLAMA_URL`, y `OLLAMA_MODEL` (si lo
fijas) tiene que ser uno de `ollama list`. Mientras tanto la herramienta usa la
primera frase del vídeo y lo dice en la cola.

### El vídeo salió sin texto

Pasó `CAPTION_MAX_WAIT_MINUTES` (10) sin texto listo y el vídeo tenía hueco: salió
con lo que tenía y quedó un aviso `caption_timeout` en Actividad. Sube ese valor
si tu máquina es lenta, o baja `WHISPER_MODEL`.

### Quiero que no genere nada

`CAPTION_AI_ENABLED=false` en `.env` y reinicia. Para un vídeo suelto, escribe
su texto y ya.

---

## Las analíticas están vacías o no cuadran

### «Sin datos todavía» con vídeos ya publicados

TikTok tarda unas horas en contar un vídeo recién publicado, y la herramienta
sólo pregunta cada 3 h. Pulsa **Actualizar ahora** en Analíticas; si sigue vacío
al día siguiente, revisa que el vídeo esté realmente en tu perfil.

Si **Actualizar ahora** contesta siempre «0 vídeos» y la pestaña nunca se llena,
mira la versión: hasta la 3.3 se leía mal la respuesta de Zernio (se buscaba un
campo `_id` que ese endpoint no manda) y todas las filas se descartaban en
silencio, sin error y sin datos. Actualiza y vuelve a pulsar; los números
aparecen en la primera consulta, también los de los vídeos ya publicados.

### Faltan «vistas de perfil» y «% de vídeo visto»

Los da TikTok **24–48 h** después de publicar, y el porcentaje de vídeo visto
sólo de los vídeos activos en los últimos 7 días. Antes de eso valen 0. Si nunca
aparecen, la cuenta está conectada por el carril antiguo: reconéctala en Zernio.

### No veo impresiones, alcance ni tiempo de visualización

No existen en ninguna API pública de TikTok. Están sólo en TikTok Studio. Ver
[ANALYTICS.md](ANALYTICS.md).

### Los vídeos que publiqué a mano no salen

Sólo se guardan los que ha publicado esta herramienta: son los únicos que se
pueden emparejar con algo local.

---

## Los comentarios

### «Esta cuenta sigue conectada por la app de desarrollador de TikTok»

El buzón sólo existe en el carril de **TikTok for Business**. Entra en el panel
de Zernio y **reconecta esa cuenta**; reconectar la misma conserva su historial.
Detalles en [COMMENTS.md](COMMENTS.md).

### Un comentario nuevo tarda en aparecer

Hasta unos 40 minutos: Zernio cachea la lista 10 minutos y la herramienta
pregunta cada 30. **Buscar nuevos** se salta la espera propia, pero no la caché
de Zernio. No hay webhooks porque esto corre en `127.0.0.1` sin URL pública.

### Un comentario antiguo ya no se actualiza

Los vídeos dejan de consultarse a los 14 días (`COMMENTS_WINDOW_DAYS`). Los
comentarios que ya estaban siguen guardados y se pueden responder.

### No puedo dar «me gusta» a un comentario

Zernio no lo soporta en TikTok, sólo en otras redes. Ocultar y responder sí.

---

## La programación no se guarda

### El botón «Guardar programación» está deshabilitado

Tienes **Declarar contenido comercial** encendido sin marcar «Tu marca» ni
«Contenido de marca». TikTok exige saber cuál de las dos es; el mensaje bajo el
bloque te lo dice.

### «El intervalo mínimo no puede ser mayor que el máximo»

Eso.

---

## Avisos

### El aviso de prueba falla

- **ntfy**: la URL debe ser `https://ntfy.sh/<tema>` y la app suscrita al mismo tema.
- **Telegram**: la URL debe llevar `?chat_id=…`. Escríbele algo al bot primero
  (un bot no puede iniciar conversaciones) y saca el `chat_id` de `getUpdates`.
- **Discord**: si el webhook se borró en el canal, Discord devuelve 404.

---

## Problemas con los archivos

### «No se pudo leer el archivo como vídeo»

ffprobe no reconoce el archivo: está corrupto, incompleto o no es un vídeo.

### «ffprobe no está instalado»

Sin ffmpeg no hay validación previa. Los vídeos se suben igual, pero los fallos
los descubrirás al publicar en vez de al añadirlos:

```bash
brew install ffmpeg          # macOS
sudo apt install ffmpeg      # Debian / Ubuntu
```

### Un vídeo copiado en `media/library/` no aparece en la biblioteca

- ¿La extensión es `.mp4`, `.mov` o `.webm`?
- ¿Se terminó de copiar? Los archivos de 0 bytes y los modificados hace menos de
  5 segundos se ignoran hasta el siguiente ciclo.
- ¿Es un duplicado? Se detectan por contenido, no por nombre: Actividad dice
  una vez con qué vídeo de la biblioteca coincide.

Fuerza una revisión con **Buscar en media/library** (página Biblioteca). Un
vídeo de la biblioteca no entra en ninguna cola por sí solo: añádelo desde la
cola con «Añadir desde la biblioteca…».

### «Duplicado de …»

Ese archivo ya está en la cola de esa cuenta, aunque se llame distinto. Para
ponerlo en otra cuenta, súbelo allí o usa **Mover a…**.

Reconversión que funciona para casi todo:

```bash
ffmpeg -i entrada.mp4 -c:v libx264 -preset slow -crf 22 \
  -vf "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2" \
  -r 30 -c:a aac -b:a 128k salida.mp4
```

---

## Base de datos

### He roto algo editando una fila

Restaura la última copia: para la app, copia `data/backups/scheduler-….db` sobre
`data/scheduler.db` y arranca. Si no tenías copia, **Actividad** registra
qué fila y qué columnas cambiaste.

### «Sólo se admiten consultas de lectura»

La consola SQL no ejecuta `UPDATE`, `DELETE`, `INSERT` ni DDL, a propósito. Edita
desde la tabla, o usa `sqlite3 data/scheduler.db` en la terminal con la app parada.

### Quiero empezar de cero

Esto borra cuentas, cola e historial. Los vídeos de `media/` no se tocan.

```bash
rm -f data/scheduler.db data/scheduler.db-wal data/scheduler.db-shm
# y si quieres empezar también con claves nuevas (tendrás que pegar la API key otra vez):
rm -rf .secrets/
```

---

## Diagnóstico a fondo

```bash
# ¿Qué dice la app de sí misma?
curl -s http://127.0.0.1:8787/api/health | python3 -m json.tool
curl -s http://127.0.0.1:8787/api/admin/system | python3 -m json.tool

# Estado completo: cuentas, cola, eventos
curl -s http://127.0.0.1:8787/api/state | python3 -m json.tool | head -60

# Documentación interactiva de la API
open http://127.0.0.1:8787/docs
```

Para ver todo lo que pasa, arranca con el log en modo depuración:

```bash
./.venv/bin/python -m uvicorn app.main:app --port 8787 --log-level debug
```

Y si sospechas de la lógica de horarios, los tests la cubren entera:

```bash
./.venv/bin/python -m pytest tests/test_planner.py -v
```
