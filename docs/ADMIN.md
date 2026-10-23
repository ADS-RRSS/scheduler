# Administración

Todo lo que hay en el menú superior más allá de la cola y la programación:
**Resumen** (el logo), **Calendario**, **Cuentas**, **Base de datos** y
**Ajustes**. Analíticas, Comentarios y Actividad van juntas en el submenú
**Seguimiento**. Pensado para llevar varias cuentas sin abrir una terminal.

> Estas páginas pueden mostrar API keys y editar la base de datos. Si alguien
> más usa tu ordenador, pon `UI_PASSCODE=algo` en el `.env`: activa una pantalla
> de acceso para toda la interfaz. Ver [SECURITY.md](SECURITY.md).

---

## Resumen

La primera pantalla, y la que abre el logo (no tiene entrada propia en el menú).
Con cuentas de las dos plataformas, arriba a la derecha se elige **Todas**,
**TikTok** o **YouTube** —se recuerda, y vale también para el Calendario—, y una
línea suma lo de las cuentas elegidas: en cola, en Zernio, hoy, publicados y
fallidos.

Una tarjeta por cuenta, con su plataforma y su color (el mismo que en el
Calendario): cuántas de sus colas están activas, el total en cola, publicados hoy
(sobre el máximo de la cuenta), publicados y fallidos, el vídeo que se está
subiendo y el último fallo si lo hay. Debajo, **una línea por cola** con su
franja, su próxima publicación y botones para abrir su cola, su programación o
pararla. **+ Nueva cola** añade otra a esa misma cuenta.

Más abajo, las próximas publicaciones de las colas elegidas ordenadas por hora
(con **Ver la semana en el calendario**), y la última actividad de esas cuentas.

## Calendario

La semana, lunes a domingo, con una columna por día y, dentro, **un carril por
cuenta**: cada publicación es una marca a su hora, del color de su cuenta.

| Marca       | Qué es                                                                  |
|-------------|-------------------------------------------------------------------------|
| Llena       | Publicado                                                               |
| Rayada      | Entregado a Zernio: publica solo a esa hora (parpadea mientras se sube) |
| Con halo    | El siguiente de una cola local, a la hora que tiene fijada              |
| Discontinua | **Previsto**: la hora que darían hoy las reglas de su cola              |
| Roja        | Fallido                                                                 |

Los **previstos** son una simulación, como «Ver próximas publicaciones», pero de
todo lo que espera en las colas activas: intervalo, franjas, días, máximos de la
cola y de la cuenta y separación mínima. Las colas de una misma cuenta se simulan
en su orden y cada una esquiva lo que ya se dio a las anteriores, como hace el
planificador de verdad. No escriben nada: cambian en cuanto cambian las reglas,
las colas o su orden. Una cola parada no prevé nada (lo dice arriba), y con la
programación pausada tampoco sale lo local.

Cada cabecera de día dice cuántas publicaciones lleva cada cuenta frente a su
máximo («12/15»; subrayado si se pasa) y, al pulsarla, lista las de ese día. Un
clic en una publicación abre su cola con el vídeo a la vista. **←** / **→** cambian
de semana, **Esta semana** vuelve, **Previstos** los quita, y la línea roja es
ahora. Se actualiza solo cada minuto.

## Selector de cuenta y de cola

Con más de una cuenta, en **Cola** y **Programación** aparece un selector de
cuenta arriba a la derecha. Justo debajo, una barra de pestañas con las colas de
esa cuenta: el punto indica si su programación está activa y el número, cuántos
vídeos esperan. La cuenta y la cola elegidas se recuerdan en el navegador.

## Colas

Una cola es una lista de vídeos con sus propias reglas de publicación, dentro de
una cuenta. Una cuenta de TikTok se conecta **una sola vez**; si quieres
publicarla con dos horarios distintos (por ejemplo 10:00–22:00 y 22:00–10:00),
le añades una segunda cola en vez de conectarla otra vez.

| Acción            | Dónde                               | Qué hace                                                                                                                                                                                                                                                                                                         |
|-------------------|-------------------------------------|------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| **+ Nueva cola**  | barra de pestañas, Resumen, Cuentas | Añade una cola vacía y parada a la cuenta.                                                                                                                                                                                                                                                                       |
| **Duplicar**      | Programación                        | Crea otra cola con las mismas reglas (intervalo, máximo, franjas, interacciones…) pero sin vídeos y parada.                                                                                                                                                                                                      |
| **Renombrar**     | Programación                        | El nombre que ves en las pestañas, los avisos y el registro.                                                                                                                                                                                                                                                     |
| **↑ ↓**           | Programación                        | Cambia el orden. El planificador recorre las colas en ese orden.                                                                                                                                                                                                                                                 |
| **Eliminar cola** | Programación                        | Abre un diálogo con dos opciones: **mover** sus vídeos a otra cola, o **borrar la cola con ellos** (con la casilla opcional de borrar también los archivos del disco). Si tiene vídeos hay que elegir una; nunca se borra nada por omisión. No se puede eliminar la última cola, ni una con una subida en curso. |
| **Mover a…**      | Cola                                | Manda un vídeo a otra cola, de esta cuenta o de otra.                                                                                                                                                                                                                                                            |
| **🔀 Mezclar**    | Cola, encima de la lista            | Ordena al azar los vídeos que esperan, otra vez en cada pulsación. La cola publica en ese orden y, si la mandas a Zernio, se entregan en ese orden. Lo que ya está saliendo («Ahora», un reintento) se queda delante; lo nuevo entra detrás. **Orden de llegada**, al lado mientras está mezclada, lo deshace.   |

Lo que comparten todas las colas de una cuenta se edita en **Cuentas**, en la
tarjeta de esa cuenta (*Límites de la cuenta*), no en Programación: no es una
regla de la cola que tengas abierta. **Programación** los recuerda en una línea,
con los avisos de cuando las colas suman más de lo que la cuenta admite.

- **Máximo de vídeos al día** en total (15 por defecto, lo que admite Zernio).
  Si las colas suman más entre ellas, manda este: las últimas del día se quedan
  sin hueco y la interfaz lo avisa.
- **Separación mínima** entre dos publicaciones, vengan de la cola que vengan.
  Es la única que hay: vale también entre dos vídeos seguidos de la misma cola,
  como suelo del intervalo aleatorio. Las colas **no** esperan su turno por
  culpa de esto; cada una coloca sus vídeos en los huecos que las demás dejan
  libres.
- La **clave de Zernio**, el interruptor de cuenta activa y el «un vídeo cada
  vez»: dos colas de la misma cuenta nunca suben a la vez.

Borrar una cola con sus vídeos se lleva también su historial: las filas de
`video_items` desaparecen y con ellas los recuentos de publicados de esa cuenta.
El registro de **Actividad** se conserva íntegro —lo que se publicó y cuándo—,
así que queda constancia aunque la cola ya no exista. Si quieres conservar los
recuentos, mueve los vídeos a otra cola en vez de borrarlos.

Si la cola tiene vídeos **entregados a Zernio**, borrarla con ellos pide marcar
además **Borrarla igualmente**: borrar las filas no cancela los posts. Con la
casilla marcada se intenta primero `DELETE /posts/{id}` con cada uno (un 404
cuenta como cancelado, que es lo que pasa si ya vaciaste la cola a mano en
Zernio) y después se borra la cola. Si Zernio no deja cancelar alguno, la cola
se borra igual y el diálogo y **Actividad** dicen cuántos siguen allí y se
publicarán.

Una cola se llena desde la **Biblioteca** («Añadir desde la biblioteca…» en la
página Cola). Un mismo vídeo no entra dos veces en una cuenta, aunque sea en
colas distintas; en una cuenta de TikTok y otra de YouTube, sí, compartiendo el
archivo.

### Entregar una cola a Zernio

**Cola → Mandar la cola entera a Zernio** calcula el hueco de cada vídeo
pendiente con las reglas de esa cola y se los deja a Zernio ya programados, de
forma que publiquen con el ordenador apagado. Lo que cambia en la administración
del día a día:

- Los vídeos entregados salen de la rotación local (estado **En Zernio**) pero
  **siguen contando** para el máximo diario de la cola y de la cuenta: son
  publicaciones que van a ocurrir.
- Un vídeo que entre después se programa **detrás del último entregado**, con el
  intervalo de la cola, para que no se cuele en medio de la tanda.
- La entrega respeta la **pausa** general: si pausas, las subidas que falten
  esperan (lo que ya está en Zernio se publica igual: allí no manda esta app).
- **Recuperar de Zernio** cancela los pendientes y los devuelve a la cola, y
  **Quitar** un vídeo entregado lo cancela allí antes de borrarlo.
- **Eliminar una cola con vídeos entregados** sólo deja moverlos a otra cola: no
  se pueden borrar sus filas, porque la fila es lo único que dice que Zernio va a
  publicarlos. Recupéralos antes si quieres deshacerte de ellos.

---

## Cuentas

Una fila por cuenta de TikTok conectada a través de Zernio, con su nombre, su
`@usuario`, el perfil de Zernio, el id de la cuenta en Zernio, cuántas colas
tiene y cuántas están activas, sus límites de cuenta, el tamaño de la cola, la
última publicación y cuándo se comprobó la clave por última vez. Debajo, una
línea por cola con su ritmo, su franja y su próxima publicación.

| Acción                               | Qué hace                                                                                                                                              |
|--------------------------------------|-------------------------------------------------------------------------------------------------------------------------------------------------------|
| **Interruptor activa / desactivada** | Una cuenta desactivada no publica, pero conserva sus colas y sus reglas. Útil para parar una cuenta sin tocar la programación.                        |
| **Probar conexión**                  | Pregunta a Zernio con la clave guardada si esta cuenta de TikTok sigue conectada. Actualiza el `@usuario` y el perfil.                                |
| **Mostrar clave**                    | Enseña la API key de Zernio guardada, con botón de copiar. **Cada consulta queda registrada en Actividad.**                                           |
| **Sustituir clave**                  | Pega una clave nueva. Se comprueba contra Zernio antes de guardarla: tiene que dar acceso a esta misma cuenta de TikTok.                              |
| **Renombrar**                        | El nombre que ves en toda la interfaz.                                                                                                                |
| **Añadir cola**                      | Otra cola para esta misma cuenta, con sus propios vídeos y horarios.                                                                                  |
| **Ir a la cola**                     | Selecciona la cuenta y abre su cola.                                                                                                                  |
| **Eliminar**                         | Borra la cuenta, su clave cifrada, todas sus colas y su historial de esta app. Los archivos de vídeo se quedan en `media/`. En Zernio no cambia nada. |

**Añadir cuenta** abre el mismo formulario de la primera vez: pegas la clave,
la herramienta lista las cuentas de TikTok de ese Zernio marcando las que ya
están conectadas, eliges las nuevas y les pones nombre si quieres. Una cuenta ya
conectada no se puede conectar dos veces: lo que quieres en ese caso es otra
**cola**.

### Una clave, varias cuentas

Una API key de Zernio da acceso a todas las cuentas de ese espacio de trabajo.
Cada cuenta guarda su propia copia cifrada de la clave, así que puedes
sustituir la clave de una sin tocar las demás (por ejemplo, si una cuenta pasa a
otro espacio de Zernio con otra clave).

---

## Base de datos

Un navegador de `data/scheduler.db` al estilo phpMyAdmin, dentro de la propia
interfaz.

**Tablas.** A la izquierda, cada tabla con su número de filas. Las que te
interesan:

| Tabla           | Qué contiene                                                                                                                               |
|-----------------|--------------------------------------------------------------------------------------------------------------------------------------------|
| `accounts`      | Las cuentas de TikTok (la columna `api_key_enc` es la clave cifrada)                                                                       |
| `schedules`     | Una fila por **cola**: su nombre, su orden y sus reglas                                                                                    |
| `video_items`   | Los vídeos y el historial (`schedule_id` dice en qué cola están; `caption_status`, `transcript` y `auto_hashtags` son el texto automático) |
| `events`        | El registro de actividad                                                                                                                   |
| `video_metrics` | Los números de cada vídeo publicado (una fila por vídeo)                                                                                   |
| `comments`      | Los comentarios de TikTok que se han leído, y las respuestas enviadas                                                                      |
| `settings`      | Ajustes globales: pausa, hashtags, webhook, versión del esquema                                                                            |

**Filas.** Paginadas (25–200 por página), ordenables pulsando la cabecera de
cada columna, y con búsqueda de texto en todas las columnas a la vez. Al pasar
el ratón por una cabecera ves el tipo de la columna.

**Editar.** Haz clic en una fila: se abre con cada columna en un campo. Guarda
los cambios o borra la fila. Reglas:

- La clave primaria no se edita.
- Las columnas cifradas (`*_enc`) se muestran ocultas y no se editan aquí:
  usa **Cuentas → Sustituir clave**.
- Los enteros y booleanos aceptan `1`/`0`, `true`/`false`; un campo vacío en una
  columna que admite nulos se guarda como `NULL`.
- Cada edición y cada borrado quedan registrados en Actividad.

**Consola SQL.** Sólo lectura: admite una sentencia `SELECT`, `WITH`, `EXPLAIN`
o `PRAGMA` sin `=`. Se ejecuta en una conexión aparte con `query_only`
activado, así que ni una consulta que se saltara la comprobación podría
escribir. Resultados limitados (200 filas por defecto, máximo 1000);
`Cmd/Ctrl + Enter` ejecuta. Ejemplos útiles:

```sql
SELECT status, COUNT(*) FROM video_items GROUP BY status;
SELECT original_name, last_error FROM video_items WHERE status = 'FAILED';
SELECT a.display_name, COUNT(v.id) AS publicados
  FROM accounts a LEFT JOIN video_items v ON v.account_id = a.id AND v.status = 'PUBLISHED'
 GROUP BY a.id;
PRAGMA table_info(schedules);
```

**Exportar CSV** descarga la tabla actual completa (con las columnas cifradas
ocultas).

Lo que el navegador **no** hace a propósito: crear o borrar tablas, ejecutar SQL
de escritura, editar claves cifradas. Para eso está la copia de seguridad y
`sqlite3` en la terminal.

### Borrar todo

Al final de la página, en rojo. Vacía **todas** las tablas —cuentas, claves,
colas, biblioteca, vídeos, analíticas, comentarios y registro— y borra de
`media/library` (y de las carpetas de antes, `media/inbox`, `media/posted` y
`media/failed`) todos los `.mp4`, `.mov` y `.webm` con sus textos. Es la forma de recuperar los gigas que ocupan vídeos
que ya no tienen
ninguna fila que los referencie.

Antes de pulsar verás cuántas filas hay en cada tabla y cuántos vídeos y cuánto
espacio se van a liberar. Después:

- **Tres comprobaciones seguidas**, cada una con una suma sencilla y una palabra
  que copiar. Se generan y se comprueban en el servidor, y **fallar una cancela
  el borrado entero**: hay que volver a empezar. Un doble clic despistado no
  llega a ninguna parte.
- **Copia automática** de la base de datos en `data/backups` justo antes de
  borrar. Es la única forma de volver atrás, así que no se borra con el resto
  (ocupa poco: los gigas eran los vídeos).
- **No borra nada fuera de esas tres carpetas**, aunque sea un `.mp4` guardado en
  otro sitio del proyecto, ni los archivos que no sean vídeo.
- **No cancela lo que ya esté programado en Zernio.** Esos vídeos se publicarán
  igualmente: el aviso te dice cuántos son, y se recuperan antes desde
  **Cola → Recuperar de Zernio**.
- Se niega si hay una subida en marcha; espera a que termine.

---

## Ajustes

**Hashtags globales.** Los que se barajan en `[hashtags]`.

**Avisos.** Una URL de Discord, Slack, ntfy o Telegram; se reconoce el servicio
por la URL. **Enviar aviso de prueba** guarda y prueba lo que hayas escrito.

**Copias de la base de datos.** Con la app abierta se hace **una al día sola**
(`data/backups/scheduler-auto-AAAAMMDD-HHMMSS.db`; se guardan las
`BACKUP_KEEP` = 14 últimas, cada `BACKUP_AUTO_HOURS` = 24 h). **Crear copia
ahora** genera otra, `scheduler-AAAAMMDD-HHMMSS.db`, que nunca se borra sola.
Todas usan la API de copia en caliente de SQLite (consistente aunque el
planificador esté escribiendo) y la lista dice cuáles son automáticas. Cada copia
se puede **descargar** o **borrar**. Para restaurar: para la app y sustituye
`data/scheduler.db` por la copia (y borra `scheduler.db-wal` y `-shm`, que se
aplicarían encima). Las claves de la copia sólo se pueden leer con el mismo
`.secrets/encryption.key`: para llevártelo todo junto, `make snapshot`
([INSTALL.md](INSTALL.md#llevarte-tu-instalación-a-otro-mac)).

**Mantenimiento.**

- *Registro de actividad → Recortar*: deja sólo las últimas N entradas (el
  planificador ya lo limita a 2000 por sí solo).
- *Vídeos fallidos → Limpiar*: quita de todas las colas los vídeos en estado
  fallido, y opcionalmente borra sus archivos de `media/failed/`.

**Sistema.** Versión de la app y del esquema, Python y SQLite, estado del
planificador (ciclos, tiempo activo, último ciclo), ffmpeg, tamaño de la base de
datos, archivos y tamaño de cada carpeta de `media/`, vídeos por estado, la API
de Zernio en uso, si el código de acceso está activo y cuántas copias hay.

---

## Desde otro programa

Todo lo anterior es API JSON. Con la app en marcha, la documentación interactiva
está en <http://127.0.0.1:8787/docs>. Lo más útil:

| Método y ruta                                               | Para                                             |
|-------------------------------------------------------------|--------------------------------------------------|
| `GET /api/state`                                            | Todo el estado: cuentas, colas, eventos, ajustes |
| `POST /api/zernio/accounts` `{api_key}`                     | Listar las cuentas de TikTok de una clave        |
| `POST /api/accounts` `{api_key, account_ids, labels}`       | Conectar cuentas                                 |
| `PATCH /api/accounts/{id}` `{display_name, is_active}`      | Renombrar / activar                              |
| `GET` · `PUT /api/accounts/{id}/key`                        | Ver / sustituir la clave                         |
| `POST /api/accounts/{id}/test`                              | Probar conexión                                  |
| `PUT /api/accounts/{id}/schedule`                           | Guardar la programación                          |
| `POST /api/accounts/{id}/videos` (multipart)                | Subir vídeos                                     |
| `PATCH /api/videos/{id}` `{caption, priority, account_id}`  | Editar o mover                                   |
| `POST /api/videos/{id}/publish-now`                         | Saltarse la cola                                 |
| `PATCH /api/videos/{id}` `{hashtags}`                       | Cambiar los hashtags de un vídeo                 |
| `PATCH /api/videos/{id}` `{is_aigc}`                        | Etiqueta de IA de ese vídeo (`null` = la cola)   |
| `POST /api/schedules/{id}/shuffle`                          | Mezclar el orden de los vídeos que esperan       |
| `POST /api/schedules/{id}/unshuffle`                        | Volver al orden de llegada                       |
| `GET /api/schedules/{id}/delegation`                        | Simular la entrega: hora de cada vídeo           |
| `POST /api/schedules/{id}/delegate` `{limit}`               | Entregar la cola a Zernio                        |
| `POST /api/schedules/{id}/delegation/cancel`                | Recuperar toda la entrega                        |
| `POST /api/videos/{id}/undelegate`                          | Recuperar un vídeo entregado                     |
| `POST /api/videos/{id}/caption/regenerate`                  | Volver a escribir su texto                       |
| `GET /api/analytics?account_id&top`                         | Totales, ranking y medias por hora y día         |
| `POST /api/analytics/refresh?account_id`                    | Pedir los números a Zernio ahora                 |
| `GET /api/comments?account_id&video_id&pending&limit`       | Comentarios guardados                            |
| `POST /api/comments/refresh?account_id`                     | Buscar comentarios nuevos ahora                  |
| `POST /api/comments/{id}/reply` `{message}`                 | Responder a un comentario                        |
| `POST /api/comments/{id}/hide` `{hidden}`                   | Ocultar o volver a mostrar                       |
| `POST /api/comments/{id}/answered` `{answered}`             | Marcar respondido (o no) sin escribir nada       |
| `GET /api/events?level&account_id&kind&q&limit`             | Actividad filtrada                               |
| `POST /api/admin/backups` · `GET /api/admin/backups/{name}` | Copias                                           |
| `GET /api/admin/system`                                     | Información del sistema                          |
| `GET /api/admin/wipe/preview`                               | Qué se llevaría por delante un borrado total     |
| `POST /api/admin/wipe/challenge` · `/answer` · `/wipe`      | Las tres comprobaciones y el borrado             |
| `GET /api/db/tables/{tabla}?page&per_page&sort&dir&q`       | Navegar una tabla                                |
| `POST /api/db/query` `{sql, limit}`                         | SQL de sólo lectura                              |
