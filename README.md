# Scheduler

> Antes se llamaba **TikTok Scheduler**. En el espacio `_RRSS` publica lo que genera
> [ContentBuilder](https://github.com/ADS-RRSS/content-builder), con su ficha `.txt`.

Programador local de subidas a TikTok (y YouTube). Subes tus vídeos una vez a la
biblioteca, los pasas a una cola, defines unas reglas (cada cuánto, cuántos al
día, en qué franjas) y la herramienta los publica sola, **en público y sin app
propia de TikTok**, a través de [Zernio](https://zernio.com). Todo corre en tu ordenador; la única cuenta
externa es la de Zernio, gratis hasta dos cuentas de TikTok.

```
 tus vídeos ──▶ biblioteca ──▶ cola + reglas ──▶ Zernio ──▶ TikTok / YouTube
 (carpetas)     media/library   (esta app)        (su app auditada)
                textos ya escritos para cada plataforma
```

---

## Índice

| Documento                                          | Para qué                                                                     |
|----------------------------------------------------|------------------------------------------------------------------------------|
| **[Cómo se usa](#cómo-se-usa-how-to)** (abajo)     | Guía paso a paso desde cero                                                  |
| [docs/INSTALL.md](docs/INSTALL.md)                 | Mac nuevo, `make`, cambiar de Mac (instantáneas), Docker y VPS               |
| [docs/ZERNIO.md](docs/ZERNIO.md)                   | Zernio: qué es, límites, precios, errores y por qué es la única vía gratuita |
| [docs/ADMIN.md](docs/ADMIN.md)                     | Administración: cuentas, API keys, copias, navegador de base de datos        |
| [docs/FLOW.md](docs/FLOW.md)                       | Diagramas del funcionamiento interno                                         |
| [docs/CAPTIONS.md](docs/CAPTIONS.md)               | Textos y hashtags automáticos: Whisper, Ollama y cómo se eligen los hashtags |
| [docs/ANALYTICS.md](docs/ANALYTICS.md)             | Analíticas: qué números hay, cuáles no existen y cómo leerlos                |
| [docs/COMMENTS.md](docs/COMMENTS.md)               | Comentarios: leerlos y responderlos desde aquí                               |
| [docs/FAQ.md](docs/FAQ.md)                         | Preguntas frecuentes                                                         |
| [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | Cuando algo falla                                                            |
| [docs/SECURITY.md](docs/SECURITY.md)               | Dónde se guardan las claves y cómo                                           |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)       | Estructura del código                                                        |
| [docs/ROADMAP.md](docs/ROADMAP.md)                 | Mejoras hechas y pendientes, con su motivo y su puntuación                   |
| [CHANGELOG.md](CHANGELOG.md)                       | Qué ha cambiado                                                              |

---

## Qué hace

**Programación**

- **Intervalo con rango aleatorio** entre publicaciones (25–35 min, 55–75 min, …).
  Cuatro presets razonables y valores libres si quieres otra cosa.
- **Máximo de vídeos al día**, o sin máximo.
- **Franjas horarias** en hora de Madrid (o la zona que elijas), incluidas franjas
  que cruzan medianoche como 22:00–06:00. Por defecto: indiferente.
- **Días de la semana** activables uno a uno.
- **Jitter humano**: las publicaciones nunca caen en horas redondas exactas, y lo
  que espera a que abra una franja sale entre 0 y 30 min después de abrirse, no en
  punto.
- **Simulador de huecos**: ves las próximas 12 publicaciones antes de activar nada.
- **Calendario semanal**: la semana de todas las cuentas, cada una en su carril y
  con su color —lo publicado, lo que está en Zernio, lo programado y lo que las
  reglas de cada cola darían a lo que espera—, con los totales por día y cuenta
  frente a su máximo. Se ven los huecos y dos cuentas a la misma hora; un clic en
  una publicación abre su cola con el vídeo a la vista.

- **Varias colas por cuenta**: la misma cuenta de TikTok puede tener una cola de
  10:00 a 22:00 y otra de 22:00 a 10:00, cada una con sus vídeos, su ritmo y su
  modo de publicación. Los límites de la cuenta (máximo diario y separación
  mínima) los comparten todas.

**Publicación**

- **Vía Zernio**: publicación directa y **siempre pública**, que es lo único que
  publica la app de TikTok for Business por la que conecta Zernio.
- **Dos formas de publicar la misma cola**: dejando la app abierta, que va
  soltando los vídeos uno a uno cuando toca; o **mandando la cola entera a
  Zernio de golpe**, con la hora de cada vídeo ya calculada, y cerrando el
  ordenador. Un botón, y se puede deshacer mientras no se hayan publicado.
- **Etiqueta de contenido generado por IA automática**: cada vídeo nace marcado
  (es el valor por defecto de la cola) y se le puede quitar uno a uno.
- **Interacciones y declaración de contenido comercial** por cola, tal y como
  las exige TikTok.
- **Reintentos inteligentes**: distingue entre "esto se arregla solo" (límite de
  peticiones, red) y "esto no se arregla nunca" (archivo malo, clave revocada).

**Después de publicar**

- **Analíticas**: reproducciones, me gusta, comentarios y compartidos de cada
  vídeo, con el ranking de los mejores y la media por hora y por día de la
  semana. Detalles y límites en [docs/ANALYTICS.md](docs/ANALYTICS.md).
- **Comentarios**: los de tus vídeos recientes, con respuesta y ocultar desde la
  propia herramienta. Ver [docs/COMMENTS.md](docs/COMMENTS.md).

**Cola**

- **Validación previa con ffprobe**: duración, resolución, FPS, códec, audio y
  tamaño se comprueban antes de subir.
- **Biblioteca común** (`media/library/<serie>/`): cada vídeo se sube **una
  vez**, aunque salga en TikTok y en YouTube, así que ocupa lo que ocupa una
  vez. Se suben carpetas enteras (arrastrándolas o con **Elegir carpeta…**) y
  cada `.mp4` va con el `.txt` de su mismo nombre (y su portada `.jpg` o
  `_portada.jpg`, que YouTube recibe como miniatura y TikTok como portada) a la carpeta de su serie
  (`reel_07_pensamientos-estoicos_…` → `pensamientos-estoicos`). Al entrar se
  escriben sus textos **para cada plataforma**: título, descripción, hashtags y
  comentario fijado de TikTok y de YouTube, editables allí mismo.
- **Las colas eligen de la biblioteca**: «Añadir desde la biblioteca…» copia
  los textos de la plataforma de esa cuenta y no procesa nada más, salvo que
  marques «Escribir textos nuevos para esta cola». Mientras no los edites en la
  cola, un cambio en la biblioteca llega solo a los vídeos que aún no han salido.
- **Carpeta vigilada**: lo que copies a mano en `media/library/` entra solo en
  la biblioteca (un archivo suelto en la raíz se mueve a la carpeta de su
  serie). Espera a que el archivo termine de copiarse.
- **Texto y hashtags automáticos**: un vídeo que entra sin texto se transcribe
  en local con Whisper y un modelo local vía Ollama escribe la descripción
  (gancho, dato y pregunta) y hasta 5 hashtags del tema, ordenados por lo que ya
  funcionó en tu cuenta. Si el vídeo trae su **ficha `.txt` de ContentBuilder**
  con los textos de TikTok y YouTube ya escritos (formato 2), sale con ellos tal
  cual, sin escucharlo: la duración dice que la ficha es suya. El interruptor
  **«Reescribir los textos con IA»** de la Biblioteca (apagado por defecto) hace
  que Ollama los escriba de nuevo a partir de la ficha. Se ve en la cola cómo
  va. [docs/CAPTIONS.md](docs/CAPTIONS.md).
- **Plantillas de texto** con variantes `{a|b|c}` y campos `[fecha] [hora] [dia]
  [archivo] [hashtags]`, editables vídeo a vídeo, para cuando prefieras escribirlo tú.
- **Un editor que revisa mientras escribes**: los límites de cada plataforma, la
  línea que se ve antes de «más», qué cinco hashtags cuenta TikTok (con los fijos
  de Ajustes), las mismas comprobaciones que el generador aplica a sus borradores,
  una vista previa de lo que sale, **🪄 Arreglar lo automático**, hashtags como
  chips con sugerencias (los que funcionan, los de la serie, los del tema) y
  **✨ Proponer con IA** para pedir otra versión sin guardar nada.
- **La biblioteca señala los textos con algo que corregir** («⚠ 3», y el filtro
  **Con avisos**), se filtra por **textos ya escritos, escribiéndose o fallidos**, las **carpetas
  llevan etiquetas de color** (el color lo eliges tú) para reconocerlas, filtrarlas
  (también en negativo, o las que no tienen ninguna) y añadir de una vez a una cola
  todas las que lleven una, la columna de carpetas tiene su buscador y el listado
  el suyo, y los **hashtags se editan en bloque** en una selección o una
  carpeta entera.
- **Vista previa** de cada vídeo, **mover** vídeos entre colas y cuentas,
  **publicar ahora**.
- **Empezar a publicar** ahora, mañana, el lunes que viene o el día y la hora
  que elijas en el calendario: hasta entonces la cola no reserva ninguna hora.
- **🔀 Mezclar** el orden de la cola al azar, tantas veces como quieras; si
  luego la mandas a Zernio, los vídeos van en ese orden.
- **Cambiar el turno a mano** en Pendientes: arrastra un vídeo por su asa **⠿**
  (si está seleccionado, se llevan juntos todos los seleccionados), o pulsa su
  número de turno y escribe otro (Enter). Para muchos de golpe, selecciónalos
  (por ejemplo, filtra una carpeta y marca **Todos**) y usa **⤒ Al principio**,
  **⤓ Al final** o **# Al turno…**: van juntos y en el orden que tenían. Lo que
  ya está saliendo («Ahora» o un reintento) sigue delante y no se mueve, y
  **Orden de llegada** deshace también el orden puesto a mano.

**Administración**

- **Varias cuentas de TikTok**, cada una con sus colas, sus reglas y su clave, y
  un **Resumen** con todas de un vistazo (se abre con el logo), filtrable por
  plataforma: TikTok, YouTube o todas.
- **Cuentas**: probar la conexión, ver o sustituir la API key de Zernio,
  renombrar, activar o desactivar.
- **Navegador de base de datos** tipo phpMyAdmin: tablas, búsqueda, edición de
  filas y consola SQL de sólo lectura.
- **Copias de seguridad** de la base de datos: una **automática al día** (se
  guardan las 14 últimas), las que hagas con un clic, y **`make snapshot`** para
  llevártelo todo a otro Mac (base de datos, clave de cifrado y `.env`).
  Mantenimiento: recortar el registro, limpiar fallidos.
- **Una sola copia de la app por base de datos**: una segunda (en otra terminal
  o en Docker) se niega a arrancar, en vez de publicar cada vídeo dos veces.
- **Borrar todo**: vacía la base de datos y borra los vídeos de `media/`, tras
  tres comprobaciones seguidas y con una copia automática antes.
- **Avisos al móvil** por Discord, Slack, ntfy o Telegram.
- **Claves cifradas** en disco, **código de acceso** opcional y **migración
  automática** de la base de datos al actualizar.

---

## Requisitos

|        |                                                                               |
|--------|-------------------------------------------------------------------------------|
| Python | 3.11 o superior (probado en 3.11 y 3.13; el `python3` de Xcode, 3.9, no vale) |
| ffmpeg | recomendado (`brew install ffmpeg`) — sin él no hay validación previa         |
| Zernio | una cuenta gratis en [zernio.com](https://zernio.com) con tu TikTok conectado |
| Ollama | opcional (`brew install ollama`, `ollama pull gemma4:26b`) — mejores textos   |

En un Mac nuevo, `make bootstrap` instala Python, ffmpeg y Ollama con Homebrew
(`Brewfile`) y prepara la app; `make doctor` dice si falta algo. Docker es
opcional, sobre todo para un equipo siempre encendido: [docs/INSTALL.md](docs/INSTALL.md).

---

## Cómo se usa (HOW TO)

### Paso 1 · Zernio (5 minutos)

1. Crea una cuenta en <https://zernio.com>. No pide tarjeta.
2. **Connect account → TikTok** y autoriza tu cuenta en la pantalla de TikTok.
   Repite si tienes varias cuentas (las dos primeras son gratis).
3. **Dashboard → API keys → Create**. Copia la clave: es lo único que
   necesita esta herramienta.

Qué hace Zernio, sus límites y sus precios: [docs/ZERNIO.md](docs/ZERNIO.md).

### Paso 2 · Arrancar

```bash
make run        # o ./run.sh, que es lo mismo
```

La primera vez busca un Python 3.11 o superior, crea el entorno virtual, instala
las dependencias y genera un `.env` con valores por defecto que sirven. Después
abre <http://127.0.0.1:8787>. `make` a secas enseña todos los atajos.

> Si compartes el ordenador, pon un `UI_PASSCODE` en el `.env`: las páginas de
> administración pueden mostrar API keys y editar la base de datos.

### Paso 3 · Conectar

Pega la API key, pulsa **Buscar mis cuentas de TikTok**, marca las que quieras
gestionar y **Conectar seleccionadas**. La clave se guarda cifrada en tu disco y
sólo viaja hacia Zernio.

Más cuentas después: **Cuentas → Añadir cuenta**. Una misma cuenta de TikTok se
conecta una sola vez; para publicarla con varios horarios, dale varias colas
(paso 4 bis).

### Paso 4 · Programación

Con la cuenta elegida arriba a la derecha y la cola elegida en la barra de
pestañas, en **Programación**:

Arriba, una línea recuerda el techo que comparten **todas** las colas de la
cuenta —máximo de vídeos al día y separación mínima entre dos publicaciones—
porque manda sobre lo que pongas aquí. No se edita en esta pantalla: es una
propiedad de la cuenta, no de la cola, y vive en **Cuentas**.

**1 · Cada cuánto se publica.** Un preset o tu rango. Se sortea un tiempo
aleatorio dentro del rango en cada publicación.

| Preset      | Rango       | Para                                  |
|-------------|-------------|---------------------------------------|
| Ráfaga      | 25–35 min   | Empujar una cuenta nueva              |
| Frecuente   | 55–75 min   | Ritmo alto sostenible                 |
| Equilibrado | 150–210 min | Lo más seguro en cuentas consolidadas |
| Pausado     | 300–420 min | 2–3 vídeos al día                     |

**2 · Máximo al día.** Un número, o *Sin máximo*, sólo para esta cola. Manda
siempre el techo de la cuenta: si las colas suman más, se quedan cortas.

**3 · Franja horaria.** Por defecto **indiferente**. Presets (06:00–21:00,
10:00–22:00, 22:00–06:00, picos) o las tuyas.

**4 · Días de la semana.**

**5 · Cómo se publica.** Todo sale **en público y directo**; no hay nada que
elegir ahí porque es lo único que publica la app de TikTok for Business. Lo que
sí configuras: interacciones (comentarios, dúos, stitch), etiqueta de contenido
generado por IA y declaración de contenido comercial.

**Ver próximas publicaciones** simula los próximos 12 huecos, contando lo que las
demás colas ya tienen reservado. Luego **Guardar**.

### Paso 4 bis · Varias colas en la misma cuenta

Una cola es una lista de vídeos con sus propias reglas. Con **+ Nueva cola**
(en la barra de pestañas de **Cola** y **Programación**, o en **Cuentas**)
añades otra a la misma cuenta de TikTok, sin volver a conectarla ni gastar otra
cuenta de Zernio. El caso típico:

| Cola  | Franja        | Para                                |
|-------|---------------|-------------------------------------|
| Día   | 10:00 – 22:00 | El contenido principal              |
| Noche | 22:00 – 10:00 | Reposiciones, otro tono, otro ritmo |

- **Duplicar** crea una cola con las mismas reglas: cambia sólo la franja.
- Cada cola se activa y se para por separado, y tiene su propio máximo diario.
- Cada cola se llena desde la **biblioteca**; un mismo vídeo no puede estar en
  dos colas de la misma cuenta (saldría dos veces), pero sí en una de TikTok y
  otra de YouTube, compartiendo el archivo.
- Las colas **funcionan a la vez**: una cola nueva coloca sus vídeos desde hoy,
  intercalándolos entre los que las demás ya tienen reservados. Lo que las
  mantiene apartadas es la **separación mínima** de la cuenta.
- A la hora de subir sí hay turno: **un vídeo cada vez por cuenta**, y sale el
  del hueco más antiguo. El máximo diario de la cuenta se reparte entre todas.
- Al **eliminar** una cola, un diálogo te deja elegir: mover sus vídeos a otra
  cola, o borrarla con todos ellos. Los archivos siguen en la biblioteca (se
  borran desde allí) y el registro de **Actividad** no se toca.

### Paso 5 · Vídeos

**Biblioteca**: arrastra la carpeta (solo cuentan los vídeos, `.txt` y
portadas `.jpg`/`.png` que tiene directamente, no sus subcarpetas), o copia los
`.mp4` con su `.txt` y su portada a `media/library/`. Con varias carpetas (arrastradas
juntas, o con **Varias carpetas…**) eliges a dónde van: cada una a la suya, todas a una
que ya existe o todas a una nueva; una carpeta sin vídeos propios aporta sus subcarpetas. La portada (`<vídeo>.jpg` o
`<vídeo>_portada.jpg`) va a YouTube como miniatura personalizada y a TikTok como portada
(`video_cover_image_url`); a TikTok se le pide además el fotograma 0, porque Zernio
toma por defecto el del segundo 1. Un `.txt` escrito a mano es el texto del vídeo
en todas las
plataformas; la ficha de ContentBuilder trae los de cada plataforma, que se usan
tal cual salvo que actives «Reescribir los textos con IA». Luego,
en **Cola**, «Añadir desde la biblioteca…» y elige los vídeos (o la carpeta
entera). El texto admite variantes y campos:

```
{Mira esto|No te lo pierdas|Atención} 👀 [hashtags]
Publicado el [dia] [fecha] a las [hora]
```

| Campo        | Da                                          |
|--------------|---------------------------------------------|
| `[fecha]`    | 15/09/2026                                  |
| `[hora]`     | 09:02                                       |
| `[dia]`      | martes                                      |
| `[mes]`      | septiembre                                  |
| `[archivo]`  | nombre del archivo sin extensión            |
| `[hashtags]` | los hashtags globales de Ajustes, barajados |

Debajo del nombre de cada vídeo se ve **en qué cola está** y en qué estado
(«📋 En Relay User · Cola principal · en Zernio», o «No está en ninguna cola»);
al pulsarlo se abre esa cola con el vídeo a la vista. El desplegable de la
barra filtra por cola («En alguna cola», «En ninguna cola» o una concreta), y
**Todas las carpetas** lista la biblioteca entera.

Un vídeo cuyos textos tienen algo que corregir lleva **⚠ N** junto al nombre
(al pulsarlo se abren sus textos), y **Con avisos** deja sólo esos: con cientos
de vídeos es la forma de revisar lo que escribió el modelo sin abrirlos uno a
uno. **# Hashtags…** añade o quita hashtags en los seleccionados o en toda la
carpeta (los añadidos van delante, que es lo que cuenta TikTok), y **Reescribir**
en un vídeo suelto pasa delante de los que se están escribiendo en bloque.

Una carpeta se renombra con **✏️ Renombrar**, junto a su nombre en la
Biblioteca: cambia a la vez el directorio de `media/library/` y la base de datos
(la biblioteca y las filas de las colas, también las publicadas), y los vídeos de
esa serie que subas después entran en la carpeta nueva. No se deja mientras uno de
sus vídeos se esté subiendo o escuchando, ni con el nombre de otra carpeta.

Para cambiar vídeos sueltos de carpeta, **📁 Mover…** (en la barra, para los
seleccionados, o en cada vídeo) los lleva a otra carpeta o a una nueva, con su
`.txt` y su portada; si el nombre ya está ocupado allí se renombra `-1`, `-2`…
El destino se elige como en la columna Carpetas: la tabla de etiquetas (✓ / ✕)
y un buscador por nombre; un nombre que no es ninguna carpeta ofrece **＋ Crear**
(nunca se elige solo, para que una errata no cree una carpeta).
Las colas que los tienen siguen publicándolos desde la carpeta nueva, una
carpeta que se queda vacía desaparece (con sus etiquetas), y los vídeos de esa
serie que subas después siguen entrando en su carpeta de siempre. Un vídeo que
se está subiendo o escuchando se queda donde está y se avisa.

La lista de la cola se filtra por carpeta con el desplegable junto a los
filtros de estado. Los vídeos que esperan se marcan uno a uno (o con **Todos**)
para **Quitar N de la cola** de una vez; siguen en la biblioteca.

Cada vídeo tiene **Ver**, **Texto**, **Ahora**, **🤖 IA**, **Mover a…** y
**Quitar**. **Texto** abre el editor con el título, la descripción, los hashtags
y el comentario fijado, que revisa mientras escribes y enseña cómo saldrá el post
([docs/CAPTIONS.md](docs/CAPTIONS.md#editar-los-textos)). El botón **🤖 IA**
encendido significa que ese vídeo se publicará declarado como contenido generado
por IA; se apaga vídeo a vídeo y el valor con el que nacen lo pone la cola
(**Programación → 5 · Cómo se publica**), activado por defecto.

### Paso 6 · Activar

**Programación → Programación activa**. El punto verde arriba a la derecha
indica que el planificador está en marcha; **Pausar** lo detiene todo sin perder
nada. El **Resumen** (el logo, arriba a la izquierda) muestra todas las cuentas
—o sólo las de TikTok o las de YouTube—, las próximas publicaciones y la última
actividad; **Calendario**, la semana entera hora a hora. Analíticas, Comentarios
y Actividad están bajo **Seguimiento** en el menú.

### Paso 7 · O manda la cola a Zernio y cierra el programa

En **Cola → Mandar la cola entera a Zernio**. Calcula la hora de cada vídeo con
las reglas de esa cola —el mismo intervalo, las mismas franjas, el mismo máximo
diario— y te enseña la lista completa antes de enviar nada: cuántos días ocupa,
la separación entre uno y otro y la hora exacta de cada uno.

Al confirmar, los vídeos salen de la cola local y se suben a Zernio **de uno en
uno** (deja la app abierta hasta que la barra llegue al final; puedes seguirlo en
el filtro **En Zernio**). A partir de ahí publica Zernio a la hora indicada
aunque el ordenador esté apagado.

- Con una carpeta elegida en el desplegable de la lista, el botón manda **sólo
  esa carpeta**; el resto de la cola se queda aquí y empieza detrás de ella.
- Se puede deshacer mientras no se hayan publicado: **Recuperar de Zernio**
  borra allí el post (`DELETE /posts/{id}`) de los que queden y los devuelve a la
  cola —todos, o sólo los de la carpeta elegida—, **Recuperar** lo hace con un
  vídeo suelto, y en el filtro **En Zernio** se marcan varios (o **Todos**) para
  **Recuperar N de Zernio** de una vez.
- Los vídeos que dejes caer después siguen la cola normal, y su hueco se calcula
  **después** del último que esté en Zernio.
- Si Zernio se queda sin cupo diario (15 vídeos por cuenta cada 24 h), no los
  descarta: los retiene y los suelta cuando la ventana lo permite.

---

## Estructura de carpetas

```
media/
├── library/                   ← la biblioteca: cada vídeo una vez (se vigila sola)
│   ├── pensamientos-estoicos/ ← una carpeta por serie, con cada .mp4, su .txt y su .jpg
│   └── …
└── inbox/ posted/ failed/     ← de antes de la biblioteca; se vacían con
                                 tools/library_copy.py + tools/library_finalize.py
data/
├── scheduler.db          ← colas, cuentas, métricas y comentarios (claves cifradas)
├── hashtags.json         ← banco de temas para los hashtags automáticos (edítalo)
├── models/whisper/       ← pesos de Whisper, descargados la primera vez (~460 MB)
└── backups/              ← copias de la base de datos (una al día sola, las de Ajustes y make snapshot)
.secrets/
└── encryption.key        ← clave que cifra las API keys (permisos 600)
```

### Pasar a la biblioteca lo que ya tenías

Los vídeos de antes de la biblioteca vivían una vez por cola en `media/inbox`,
`posted` y `failed`. Se pasan en dos pasos, y sin el segundo no se borra nada:

```bash
./.venv/bin/python tools/library_copy.py            # dice qué copiaría
./.venv/bin/python tools/library_copy.py --apply    # copia y registra
./.venv/bin/python tools/library_finalize.py        # comprueba la copia
./.venv/bin/python tools/library_finalize.py --apply  # con la app cerrada
```

`library_copy.py` copia cada vídeo **una vez** a la carpeta de su serie, con su
`.txt`, le da a la biblioteca los textos que ya tenían sus colas (no transcribe
nada) y enlaza las filas de las colas. En un Mac la copia es un **clon APFS**:
no ocupa espacio hasta que se borra el original (`du` cuenta los dos; `df`
dice la verdad). Puede ejecutarse con la app abierta y repetirse.

`library_finalize.py` compara byte a byte cada original con su copia; sólo si
es idéntica cambia la ruta de las filas de `video_items` a la biblioteca y
borra el original. Lo que no puede verificar se queda donde está y lo lista.
Antes de tocar nada guarda una copia de la base de datos en `data/backups`.

---

## Preguntas rápidas

**¿Tengo que dejar el ordenador encendido?** Depende de cómo publiques. Si
gestiona la app, sí: es local, y si la cierras no publica (al reabrir retoma la
cola donde estaba). Si mandas la cola a Zernio (paso 7), no: publica Zernio y el
ordenador puede estar apagado. Para tenerlo siempre en marcha sin delegar, un
VPS pequeño o un mini-PC con Docker (`docker compose up -d`, ver
[docs/INSTALL.md](docs/INSTALL.md#docker-merece-la-pena)).

**¿Y si cambio de Mac?** `make snapshot` en el viejo, copia `media/` y
`make restore FILE=…` en el nuevo. Los pasos, en [docs/INSTALL.md](docs/INSTALL.md#llevarte-tu-instalación-a-otro-mac).

**¿Qué pasa si borro la base de datos con vídeos en Zernio?** Se publican
igualmente: Zernio ya los tiene. Recupéralos antes desde la Cola si no quieres
que salgan; el botón de borrar te avisa de cuántos hay.

**¿Zernio es de fiar?** Es un tercero que verá tus vídeos y textos y publicará en
tu nombre con la autorización que tú le des en TikTok. Gratis hasta dos cuentas,
pago por cuenta a partir de ahí. Puedes revocar la clave y la conexión con TikTok
en cualquier momento. Detalles en [docs/ZERNIO.md](docs/ZERNIO.md).

**¿Y si pierdo la API key?** **Cuentas → Mostrar clave** te la enseña (queda
registrado en Actividad). Si la revocaste en Zernio, **Sustituir clave**.

**¿Me pueden banear?** Zernio usa la API oficial de TikTok con tu autorización,
que es lo contrario de un bot. El riesgo real es el ritmo: publicar cada 5 minutos
llama la atención venga de donde venga. Los presets están en rangos sensatos.

---

## Tests

```bash
make test       # pytest; make lint (ruff) y make check (los dos)
make docker-test   # lo mismo dentro de la imagen Docker (Linux)
```

Incluyen el flujo completo contra un Zernio simulado (`tools/mock_zernio.py`),
que exige el `Bearer` correcto, la subida presignada con `Content-Length`, los
indicadores de consentimiento, y devuelve la URL de TikTok con retraso, como el
real. `./tools/demo.sh` arranca la app contra ese mismo Zernio falso para
probarlo todo sin cuenta (clave: `zernio-test-key`).

---

## Licencia

Uso personal o profesional bajo tu responsabilidad. Respeta los
[Términos de Servicio de TikTok](https://www.tiktok.com/legal/page/global/terms-of-service/)
y las condiciones de Zernio.
