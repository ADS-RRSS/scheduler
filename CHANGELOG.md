# Cambios

## Sin publicar

### Añadido

- **Subir varias carpetas a la Biblioteca y elegir a dónde van.** Al arrastrar
  más de una carpeta, o con el botón nuevo **Varias carpetas…** (el navegador
  deja elegir una cada vez: se van añadiendo a una lista, o se arrastran sobre
  ella), un diálogo enseña cada carpeta con sus vídeos, `.txt` y portadas y
  pregunta a dónde van: **cada una a la suya, como hasta ahora** (la serie del
  nombre o, si no la dice, el nombre de la carpeta; el diálogo adelanta cuál y
  si es nueva), **todas a una carpeta que ya existe** (el mismo buscador con
  etiquetas de «Mover…») o **todas a una carpeta nueva**. Una sola carpeta
  sigue subiendo directa, como siempre. Elegir (o arrastrar) una carpeta que no
  tiene vídeos propios toma sus subcarpetas, un nivel, como si se hubieran
  elegido una a una. Los vídeos que ya estaban en la biblioteca no cambian de
  carpeta. `into` en `POST /api/library/upload` y `aliases` en
  `GET /api/library`.
- **La dirección dice en qué cuenta y cola estás.** Cola y Programación
  llevan la cuenta y la cola en la URL (`#/cola/<cuenta>/<cola>`, con el
  usuario de la cuenta y el nombre de la cola en minúsculas y con guiones): al
  recargar, o al abrir ese enlace en otra pestaña, se abre esa misma cola.
  Cambiar de cola o de cuenta deja una entrada en el historial (Atrás vuelve a
  la anterior); renombrar la cola sólo corrige la dirección. Un enlace con una
  cuenta o una cola que ya no existe abre la que estaba elegida.
- **Las franjas ya no publican en su hora de apertura clavada.** Cuando el
  siguiente hueco cae fuera de franja (o el día está lleno) y hay que esperar a
  que abra, el vídeo sale entre 0 y 30 minutos después de la apertura, nunca
  más de un cuarto de lo que queda de franja, en vez de a las 07:00 o 18:00
  más unos segundos todos los días. La separación entre colas y los máximos se
  siguen respetando. Los vídeos ya entregados a Zernio conservan su hora.
- **Comentarios: marcar como respondido a mano** (**✓ Respondido** / **Marcar
  sin responder**), para los que contestaste desde la app de TikTok o no piden
  respuesta. Es sólo local, no escribe nada en TikTok, y una lectura posterior
  no lo deshace. `POST /api/comments/{id}/answered`.
- **Comentarios: a qué cuenta pertenece cada uno.** Plataforma y nombre de la
  cuenta conectada, en el color de su tarjeta, y un selector para ver sólo los
  de una cuenta (el contador de «sin responder» sigue al filtro).
- **Biblioteca: filtro «Cuentas».** Encima de la lista, un chip por cuenta
  conectada (con su plataforma y cuántas colas visibles tiene); se pueden marcar
  varias —el TikTok y el YouTube del mismo canal— y ninguna es «Todas». Sólo
  las colas de esas cuentas salen en el filtro de cola y en los «📋 En …» de
  cada vídeo, y «En alguna/ninguna cola» se cuenta sobre
  ellas. Se recuerda en este navegador. `accounts` (varios) en
  `GET /api/library/items`.
- **Filtros en «Añadir a una cola…».** Encima de la lista de colas: plataforma
  (Todas · TikTok · YouTube), cuentas (varias; ninguna = todas; arranca con las
  del filtro «Cuentas» de la página), un buscador por cola o cuenta y «Ocultar
  las que ya lo tienen» (activo por defecto, con cuántas son). «Marcar las
  visibles» y «Desmarcar todas». Una cola marcada nunca se esconde: lo que se
  va a añadir siempre está a la vista. Los filtros se recuerdan en este
  navegador; las colas ocultas no salen.
- **Ocultar una cola.** «🙈 Ocultar» en Programación y «Ocultar/Mostrar» en
  cada cola del Resumen. Una cola oculta desaparece de la Biblioteca (filtros,
  «Añadir a una cola…», «📋 En …», «En alguna/ninguna cola») y de «Mover a…».
  En la barra de colas pasa al final, a la derecha y atenuada con 🙈, y nunca
  es la que se abre por defecto al entrar en una cuenta. No la para: si está
  activa, sigue publicando; para eso sigue estando «Parar». `hidden` en
  `PATCH /api/schedules/{id}`.
- **«Quitar» también para un vídeo atascado en «procesando» o «subiendo».**
  Mientras una cuenta tiene un vídeo en vuelo no sube nada más, ni siquiera un
  «Ahora» o un «⚡ publicar ya»; si ese vídeo no termina nunca, la cuenta se
  queda parada. Ahora se puede quitar: si Zernio aún tiene el post, se cancela
  allí primero, y si ya lo había publicado queda como publicado. Un «subiendo»
  sólo se deja quitar pasados `status_timeout_minutes` (60 min) desde que
  empezó, para no cortar una subida real.
- **Cada vídeo de la cola dice cuándo se añadió** («añadido 13/10 15:39»;
  `created_local` en el JSON del vídeo).

### Corregido

- **Un post que Zernio ya no tiene (404) dejaba la cuenta «procesando» para
  siempre.** Se seguía preguntando por él cada minuto y, mientras, la cuenta no
  publicaba nada. Pasado el plazo normal de espera, el vídeo queda como fallido
  («Reintentar» lo vuelve a subir) y la cuenta sigue con lo demás.
- **«Zernio no confirmó la publicación 48 h después…» con la app apagada.** El
  plazo se miraba antes de preguntar a Zernio, así que reabrir la app más de
  48 h después de la hora de un vídeo delegado lo daba por fallido sin
  consultarlo, aunque Zernio lo hubiera publicado a su hora. Ahora se pregunta
  siempre primero y sólo falla lo que Zernio aún tiene pendiente. La migración
  v9 devuelve a «en Zernio» los vídeos que fallaron por esto para que se
  consulten una vez. El ancla del ritmo de la cola ya no retrocede cuando una
  puesta al día encuentra un post antiguo después de uno más reciente.

### Cambiado

- **`/api/state` ya no se pide cada 5 s pase lo que pase**: con miles de vídeos
  entregados a Zernio pesaba ~4,8 MB y costaba al servidor 0,3–1 s cada vez.
  Ahora va cada 5 s mientras usas la página (ratón o teclado en el último
  minuto) o hay un vídeo subiéndose o procesándose, cada 30 s en reposo y nada
  con la pestaña en segundo plano; al volver a ella se pide al momento.
- **Comentarios: los tuyos («Tú») no salen en la lista** salvo que marques
  **Mostrar los míos** (con cuántos hay). Nunca contaban como pendientes; ahora
  tampoco tapan los de los demás. `mine=false` y el recuento `own` en
  `GET /api/comments`.
- **El buscador de la columna «Carpetas» de la Biblioteca filtra las carpetas**
  por su nombre (sin distinguir mayúsculas ni tildes); ya no busca vídeos en
  toda la biblioteca: para eso está el buscador nuevo del listado, con «Todas
  las carpetas».
- **Subir una carpeta a la Biblioteca solo procesa sus archivos directos**: ya no
  entra en subcarpetas (visibles u ocultas, ni siquiera las recorre al arrastrar)
  y solo cuenta los vídeos y los `.txt`; `.json`, `.sh` y cualquier otro archivo
  se ignoran.

### Añadido

- **⚡ Publicar ya, fuera de turno, desde la Biblioteca.** El diálogo
  **Añadir…** deja marcar varias colas a la vez (salen marcadas las que llevan
  el nombre de la carpeta: `monoticias` → «Monoticias TikTok» y «Monoticias
  YouTube») y, con la casilla nueva, el vídeo entra en ellas y sale en el
  siguiente tick, aunque la cola esté parada. Va por encima del ritmo de la
  cola: no reinicia su intervalo, no gasta cupo del día ni aparta a los demás
  vídeos con la separación de la cuenta, y una delegación a Zernio no lo toca.
  Queda como fila de la cola (marca «⚡ fuera de turno»), así que el mismo vídeo
  no puede volver a entrar en esas cuentas. `publish_now` en
  `POST /api/schedules/{id}/videos`; columna `video_items.extra`. El botón
  «Ahora» de Cola no cambia: adelanta el turno y el ritmo cuenta desde él.

- **Ordenar a mano los vídeos pendientes de una cola**, además de 🔀 Mezclar:
  arrastrando por el asa **⠿** (los seleccionados van juntos), escribiendo el
  turno en su número, o con **⤒ Al principio**, **⤓ Al final** y **# Al turno…**
  para una selección, que es lo práctico con cientos de vídeos. La lista sigue
  al vídeo hasta su turno nuevo, y una delegación a Zernio respeta ese orden.
  `POST /api/schedules/{id}/reorder` (`position`, `before` o `after`).
- **Mover vídeos de una carpeta a otra en la Biblioteca**: **📁 Mover…** en la
  barra (los seleccionados) o en cada vídeo, a una carpeta existente o a una
  nueva. Se mueven en disco con su `.txt` y su portada, y la biblioteca y las
  filas de las colas (también las publicadas) apuntan al sitio nuevo. Una
  carpeta vacía desaparece con sus etiquetas. El destino se busca con los
  mismos filtros que la columna Carpetas (etiquetas ✓ / ✕ y nombre), y el
  buscador también crea la carpeta nueva. `POST /api/library/move`.
- **Portada de YouTube**: la imagen que acompaña a cada vídeo (`<vídeo>.jpg` o
  `<vídeo>_portada.jpg`, como la escribe ContentBuilder; también `.png`) se sube
  a la Biblioteca con él, igual que su `.txt`, y queda a su lado como
  `<vídeo>.jpg`. Al publicar en una cuenta de YouTube se sube también a Zernio
  y va como `thumbnail` del vídeo; TikTok no la recibe. Una portada subida
  sola sustituye a la que tenía su vídeo; la carpeta vigilada la mueve con él
  y borrar el vídeo la borra. En la lista sale «🖼 con portada» y el botón
  **Portada** (`GET /api/library/{id}/cover`). Ojo: Zernio documenta las
  miniaturas personalizadas solo para vídeos largos, **no para Shorts**, y
  YouTube solo las acepta en canales verificados por teléfono; se envía igual
  por si las acepta. Si la portada no se puede subir, el vídeo sale sin ella
  (queda en el log).

- **«Empezar a publicar» en cada cola**: debajo del interruptor de la
  programación, **Ahora** (lo de siempre), **Mañana**, **Lunes que viene** u
  **Otro día y hora…**: un calendario propio (semana de lunes a domingo, los
  días pasados apagados) con la hora y el minuto al lado (de 5 en 5, o exacta),
  atajos a las 00:00 y a la hora a la que abre cada franja de la cola, y debajo
  «Empieza el jueves 15 de octubre a las 18:30». Sirve también hoy a una hora
  más tarde; una hora ya pasada no se puede aplicar. Hasta ese momento (en la
  zona horaria de la cola; sin hora, desde las 00:00) la cola no reserva
  ninguna hora; entonces arranca sola con sus reglas. Lo respetan la próxima publicación, «Ver próximas publicaciones»,
  el
  Calendario y el envío a Zernio. La cola lo muestra con ⏳ en su pestaña y
  «empieza el …» en su cabecera y en el Resumen; cuando llega el día el campo
  se vacía solo. Duplicar una cola no lo copia. Si la cola ya tiene vídeos
  programados en Zernio, cambiarlo avisa de que esos no se mueven (hay que
  recuperarlos y volver a enviarlos). `start_date` y `start_time` («HH:MM», opcional) en `PUT /api/schedules/{id}`.

- **Filtro de etiquetas de la Biblioteca con «con» y «sin»**: en la columna
  «Carpetas», una fila por etiqueta con dos botones, **✓ con** (la carpeta
  tiene que llevarla) y **✕ sin** (no puede llevarla); otro clic en el mismo
  botón la quita del filtro. Así «con «v1» y «hombre», sin «mujer»» se marca en
  tres clics y se lee en la tabla. Con dos o más ✓, **Con todas / alguna** las
  ✓. La fila **sin etiquetas** suma (✓) o deja fuera (✕) las carpetas que no
  tienen ninguna. Debajo, cuántas carpetas y vídeos quedan, y **Quitar filtro**
  arriba. La misma tabla está en **Añadir desde la biblioteca** de cada cola
  (el botón resume la elección: «Todas las carpetas · 🏷️ v1 · sin mujer»).
- **El selector de carpetas y etiquetas (Añadir desde la biblioteca y el filtro
  de la Cola) va en dos columnas**: a la izquierda las etiquetas, a la derecha
  solo las carpetas que las cumplen («1 de 18 con estas etiquetas», y cuántas
  quedan ocultas); «Todas las carpetas» pasa a «Todas las de estas etiquetas».
  Al cambiar las etiquetas se desmarcan las carpetas que ya no las cumplen, así
  que ya no se queda una carpeta marcada dejando la lista vacía.
  Se recuerda como el resto del filtro (`tts.libScope`). En la API,
  `exclude` (repetible) y `unlabeled=include|exclude` en
  `GET /api/library/items`.
- **Buscador de vídeos encima del listado de la Biblioteca**: busca por nombre,
  carpeta o textos **dentro** de la carpeta abierta o de las carpetas y etiquetas
  elegidas, y se combina con los filtros de cola, textos y avisos.

- **Ancho de la interfaz ajustable**: un selector «1× · 1,5× · 2×» en la barra
  superior (1040 px por defecto, 1560 px y 2080 px). Se guarda en
  `localStorage` (`tts.uiWidth`) y se aplica antes del primer pintado, así que
  no hay que volver a elegirlo en cada recarga. La columna «Carpetas» de la
  Biblioteca crece con él (16 rem ×1, ×1,5 o ×2).

- **Fichas de ContentBuilder con los textos de cada plataforma** (formato 2): el
  `.txt` trae, ya escritos para el buscador y el feed, el título, la
  descripción, los hashtags y el comentario fijado de TikTok y de YouTube, más
  la duración del vídeo y la palabra clave. Un vídeo con su ficha sale con esos
  textos tal cual, cada plataforma con los suyos, **sin Whisper ni Ollama**: la
  duración que mide ffprobe tiene que ser la de la ficha (si no, la ficha no se
  usa y el vídeo queda en «Fallidos» con el motivo). Las fichas antiguas siguen
  valiendo: sin IA dan su título, su primera pregunta y sus hashtags.
- **«Reescribir los textos con IA»**, un interruptor en la Biblioteca,
  **apagado por defecto**, que vale para todo lo que entre (subidas, un `.txt`
  que llega después, archivos copiados a `media/library`). Encendido, Ollama
  escribe los textos de nuevo a partir de la ficha, como antes, pero sin
  escuchar el vídeo si la ficha trae su duración. Los vídeos sin ficha se
  escuchan y se escriben siempre. `POST /api/library/ai-texts`, `ai_rewrite` en
  `GET /api/library` y el campo `ai` de `POST /api/library/upload`.
- **«📄 Textos de la ficha»** en la Biblioteca: vuelve a poner a los vídeos
  elegidos los textos de su ficha (`action: "sheet"` en `POST /api/library/bulk`).

- **Calendario semanal** (nueva página): la semana de todas las cuentas con una
  columna por día y un carril y un color por cuenta, cada publicación a su hora:
  lo publicado, lo entregado a Zernio, lo programado aquí y —simulado con las
  reglas de cada cola— lo que espera. Las colas de una cuenta se simulan en su
  orden esquivando lo que ya se dio a las anteriores, como planifica la app de
  verdad, así que lo previsto respeta el máximo diario y la separación de la
  cuenta. Totales por día y cuenta frente a su máximo, la lista de un día al
  pulsar su cabecera, la línea de ahora y, con un clic en una publicación, su
  cola con el vídeo a la vista. `GET /api/calendar`.
- **Etiquetas en las carpetas de la biblioteca** («serie», «terminada», «para
  revisar»…): «🏷️ Etiquetas» junto a «Renombrar». Cada etiqueta tiene su color,
  el mismo en todas partes: puntos junto a cada carpeta, chips junto al título y
  en cada vídeo cuando se ven varias carpetas. Encima de la lista de carpetas,
  un filtro por etiqueta (se recuerda) que deja sólo las que la llevan, y
  «Todas las de «…»» lista sus vídeos juntos. Siguen a la carpeta si se
  renombra. Cada etiqueta puede tener **el color que elijas** (doce de la paleta
  o cualquiera, y «Automático» para volver al suyo), el mismo en todas las
  carpetas que la llevan. En **«Añadir desde la biblioteca»** de una cola, una
  etiqueta vale por todas sus carpetas: se elige «🏷️ serie» y «Todos los
  disponibles» las añade todas de una vez.
- **Carpetas y etiquetas combinables** en «Añadir desde la biblioteca» y en el
  filtro de carpetas de la **Cola**: un selector con casillas para varias
  carpetas (o «Todas») y las etiquetas que las acotan —«Todas» + «serie» +
  «terminada» deja sólo las series terminadas—, con «todas / cualquiera» para
  exigir todas las etiquetas o bastar con una. En la Cola, «Enviar a Zernio» y
  «Recuperar de Zernio» actúan sobre esa misma combinación, y cada vídeo de la
  lista lleva los puntos de color de las etiquetas de su carpeta. En la
  **Biblioteca**, igual: las etiquetas de la barra lateral se combinan (con
  «todas / cualquiera», y se recuerdan), y cada carpeta tiene una casilla para
  juntarla con otras en la misma vista; pulsar su nombre abre sólo esa.
  `folders`, `labels` y `match` en `GET /api/library/items`,
  `GET …/delegation`, `POST …/delegate` y `POST …/delegation/cancel`.
  `POST /api/library/folders/labels`,
  `POST /api/library/labels/color`, `GET /api/library/items?label=`.
- **La biblioteca se filtra por el estado de sus textos**: «Ya escritos»,
  «Escribiéndose» (en cola o en curso) o «Fallidos», junto al filtro de cola y
  combinable con él. `GET /api/library/items?text=written|writing|failed`.
- **El Resumen se filtra por plataforma** (Todas, TikTok, YouTube; se recuerda y
  vale también para el calendario), con los totales de lo elegido y la
  plataforma y el color de cada cuenta en su tarjeta.

- **Un editor de textos que revisa mientras escribes** («Texto» en la cola,
  «Textos» en la biblioteca). Cuenta lo que importa en cada plataforma —la
  primera línea, que es lo que se ve antes de «más»; 2.200 caracteres con los
  hashtags en TikTok; 100 en un título; 150 en un comentario de TikTok—, aplica
  en vivo las comprobaciones que el generador hace a sus borradores (otros
  alfabetos, inglés, «¿Sabías que…?», contadores «4/50», hashtags repetidos,
  arranques iguales a otro vídeo de la serie…) en tres niveles con su campo, y
  enseña el post como lo montará el publicador, con **qué cinco hashtags cuenta
  TikTok** y cuáles no. **🪄 Arreglar lo automático** resuelve sin modelo lo que
  se puede («¿¡», el contador, hashtags metidos en el texto, `#selfcare` →
  `#autocuidado`, `#fyp`) sin reescribir ninguna frase. Los hashtags son chips,
  normalizados como en el servidor, con sugerencias: los que ya funcionan en la
  plataforma, los de la serie y los del banco de temas. **✨ Proponer con IA**
  pide otra versión a Ollama al momento (desde la ficha, la transcripción o tu
  propio texto) y sólo se usa si quieres. En la biblioteca, pestañas por
  plataforma con su recuento y «Copiar de TikTok». ⌘↵ guarda; cerrar con
  cambios pregunta. Sustituye al cuadro de «Texto» y al `prompt()` de
  «Etiquetas», que desaparece.
- **La biblioteca señala los textos con algo que corregir** («⚠ 3» junto al
  nombre, con los motivos) y el filtro **Con avisos** deja sólo esos.
- **Hashtags en bloque**: **# Hashtags…** añade o quita hashtags en los vídeos
  seleccionados o en toda la carpeta, por plataforma, con los añadidos delante
  (TikTok cuenta cinco). Los que se están escribiendo no se tocan; las colas que
  siguen a la biblioteca reciben la lista nueva.
- **Lo pedido a mano pasa delante**: «Regenerar» en la cola y «Reescribir» en un
  vídeo suelto de la biblioteca se escriben los siguientes, en vez de esperar
  detrás de una carpeta entera (con 550 pendientes eran unas dos horas). La
  biblioteca y la cola dicen cuánto le queda a la cola de textos.
- **Copias automáticas**: una copia de la base de datos al día mientras la app
  está abierta, rotando las 14 últimas (`BACKUP_AUTO_HOURS`, `BACKUP_KEEP`). Las
  hechas a mano no se borran nunca solas.
- **`make`** como punto de entrada (`make` a secas enseña la lista):
  `bootstrap` (Homebrew: Python 3.13, ffmpeg y Ollama, con el nuevo `Brewfile`),
  `setup`, `run`, `demo`, `doctor`, `models`, `ollama`, `backup`, `snapshot`,
  `restore`, `test`, `lint`, `check` y los de Docker. Ver docs/INSTALL.md.
- **`make doctor`** (`tools/doctor.py`): Python y entorno, ffmpeg, `.env`, base
  de datos (integridad, esquema, quién la usa, copias), clave de cifrado, disco,
  Whisper, Ollama, la API de Zernio y la ruta de subida a R2, y el puerto, cada
  uno con su arreglo. Sólo usa la biblioteca estándar: sirve con el entorno roto.
- **`make snapshot` / `make restore`** (`tools/snapshot.py`) para cambiar de Mac:
  la base de datos (copia consistente), la clave de cifrado, `.env` y el banco de
  hashtags en un archivo con permisos 600 y sus sumas SHA-256. Restaurar se
  niega con la app abierta, guarda antes lo que sustituye y no deja un `-wal`
  viejo que estropearía la base nueva.
- **Docker, opcional**: `Dockerfile` (Python 3.13 + ffmpeg, sin root, chequeo de
  salud) y `docker-compose.yml` con los mismos `data/`, `media/` y `.secrets/`
  que la instalación nativa, el puerto sólo en 127.0.0.1 y el Ollama del Mac por
  `host.docker.internal` (en macOS Docker no ve la GPU). Perfil `demo` con
  Zernio y Ollama falsos en volúmenes propios. Los tests pasan dentro, con Python
  3.13 y 3.11.
- **CI** (GitHub Actions): ruff y pytest en Python 3.11 y 3.13, y la imagen
  Docker con sus tests y una prueba de humo. `ruff` pasa a las dependencias de
  desarrollo (`ruff.toml`: sólo errores de verdad).
- **Aviso de disco bajo** arriba (menos de 10 GB o del 5 % libre), y el disco en
  Ajustes → Sistema.

- **Biblioteca común.** Los vídeos ya no se suben a una cola sino a la
  biblioteca (`media/library/<serie>/`), **una vez**, aunque salgan en TikTok y
  en YouTube: antes cada cola guardaba su copia y la carpeta llegó a 80 GB. Se
  suben **carpetas enteras** (arrastrar o «Elegir carpeta…»), un vídeo por
  petición con su `.txt`, y cada uno va a la carpeta de su serie
  (`reel_02_pensamientos-estoicos_el-juicio-no-el-hecho` →
  `pensamientos-estoicos`). Al entrar se escriben **una vez** sus textos para
  cada plataforma —título, descripción, hashtags y comentario fijado de TikTok
  y de YouTube, con los hashtags de cada una elegidos con las analíticas de sus
  cuentas— y se editan en la página **Biblioteca**, que también los
  reescribe, los añade a una cola o los borra, uno a uno o en bloque.
- **Las colas eligen de la biblioteca.** «Añadir desde la biblioteca…» abre la
  lista por carpetas, con lo que esa cuenta ya tiene desactivado, y copia los
  textos de la plataforma de la cuenta **sin procesar nada**, salvo que se
  marque «Escribir textos nuevos para esta cola». Un vídeo añadido mientras la
  biblioteca aún escribe sus textos espera por ellos; los que sigan los de la
  biblioteca reciben sus correcciones hasta que salen hacia Zernio o se editan
  en la cola.
- **`tools/library_copy.py` y `tools/library_finalize.py`** para pasar lo que ya
  había: el primero copia (con clones APFS, sin ocupar espacio) cada vídeo de
  `media/inbox`, `posted` y `failed` a la biblioteca con los textos que ya
  tenían sus colas, y enlaza sus filas; el segundo verifica byte a byte, cambia
  la ruta de las filas a la biblioteca y sólo entonces borra el original.
- **El título de YouTube se escribe a propósito**: el modelo recibe una regla
  para el `titulo` (30–80 caracteres, sin hashtags) y, sin modelo, se usa el
  de la portada de la ficha o el gancho.

- **Renombrar una carpeta de la biblioteca** (✏️ Renombrar, junto a su nombre):
  cambia el directorio de `media/library/` y, en la misma operación, la carpeta y
  la ruta de sus vídeos en la biblioteca y en todas las filas de las colas; si la
  base de datos falla, el directorio vuelve a su nombre. El nombre se normaliza
  como los demás (`Estoicos Día a Día` → `estoicos-dia-a-dia`) y los vídeos de esa
  serie que se suban después entran en la carpeta nueva (tabla de alias en
  `settings`). Se niega mientras uno de sus archivos se está subiendo a Zernio o
  escuchando con Whisper, o si el nombre ya existe.
- **La cola se filtra por carpeta**, y con una carpeta elegida «Enviar … a
  Zernio» y «Recuperar … de Zernio» actúan sólo sobre ella (el resto de la cola
  empieza detrás). En el filtro **En Zernio** los vídeos se marcan uno a uno o con
  **Todos** para **Recuperar N de Zernio** de una vez.
- **La biblioteca dice en qué cola está cada vídeo**, justo bajo su nombre
  (antes, unas etiquetas grises al final de la fila; los que no estaban en
  ninguna no decían nada). Cada cola es un botón que abre esa cola con el filtro
  y la carpeta del vídeo y lo deja marcado. Nuevo filtro por cola (una concreta,
  en alguna o en ninguna; `queue=` en `/api/library/items`) y la entrada
  **Todas las carpetas**.
- **Quitar varios pendientes de una cola a la vez**: los vídeos que esperan
  también se marcan (uno a uno o con **Todos**, respetando el filtro y la
  carpeta) y **Quitar N de la cola** los saca en una sola operación. Los que ya
  están en Zernio o subiéndose no se tocan, y los archivos siguen en la biblioteca.

- **🔀 Mezclar el orden de una cola**, tantas veces como quieras: cada pulsación
  reparte los vídeos que esperan en un orden al azar nuevo (nunca el mismo que
  había). La cola publica en ese orden y **Mandar la cola entera a Zernio** los
  entrega en ese orden también, con la primera hora para el primero; el diálogo
  lo recuerda. «Recuperar de Zernio» los devuelve en el mismo orden, los vídeos
  que entran después se ponen a la cola detrás de los mezclados y **Orden de
  llegada** deshace la mezcla. Lo que ya está saliendo («Ahora» o un reintento)
  se queda delante. La lista de **Pendientes** se ve ahora en el orden de salida,
  con el turno de cada vídeo, y la de **En Zernio**, por la hora a la que Zernio
  publicará cada uno.
- **Borrar una cola aunque tenga vídeos entregados a Zernio.** El aviso sigue
  ahí, pero ya no es un callejón sin salida: la casilla **Borrarla igualmente**
  del diálogo intenta cancelar esos posts en Zernio (un 404, porque ya los
  borraste allí, cuenta como cancelado) y borra la cola. Lo que Zernio no deje
  cancelar se avisa en el diálogo y en Actividad en lugar de parar el borrado.
  El diálogo también cuenta ya los vídeos entregados a Zernio en el total.
- **La ficha `.txt` de ContentBuilder es la base de la descripción.** Súbela junto
  a su vídeo (mismo nombre, en la misma subida o en `media/inbox/`) y, en vez de
  publicarse tal cual con el guion entero, se lee como contexto: guion exacto,
  título, pie provisional, hashtags y preguntas del comentario fijado. Whisper
  escucha los primeros 20 s para confirmar que la ficha es de ese vídeo (medido:
  96–100 % con el suyo, 3–10 % con otro de la serie). El modelo valora el pie
  provisional —quita el «7/50», la descripción de la serie y los hashtags de
  otro reel; añade el autor o el dato del guion— y lo que cambió queda en
  Actividad. Un `.txt` que llega después encuentra su vídeo en la cola; un
  `.txt` sin esas marcas sigue siendo un texto escrito a mano. El aviso
  «⚠ revisar a mano» del verificador de ContentBuilder sale en la cola.
- **Descripciones en tres líneas: gancho, dato y pregunta**, con los hashtags
  en su propia línea. Antes era un teaser de 46 caracteres de media («¿Cuándo
  pierdes la chispa con tu pareja? 🤔») que no daba nada que buscar ni invitaba
  a comentar. El botón **Texto** abre ahora un cuadro que respeta las líneas.
- **Comprobaciones y una segunda pasada.** Lo que escribe el modelo se revisa
  (otro alfabeto, inglés, «¿Sabías que…?», líneas largas, falta el dato o la
  pregunta, arranques repetidos) y, si hace falta, el modelo recibe su propia
  respuesta con la lista de problemas para corregirla; se queda la versión con
  menos problemas graves. «¿», «¡», comillas, emojis de más y hashtags en
  inglés con equivalente claro se arreglan sin modelo. `CAPTION_EDITOR=false`
  la apaga.

### Cambiado

- **La cabecera, más corta**: Analíticas, Comentarios y Actividad van en el
  submenú **Seguimiento** (con el número de comentarios sin responder a la
  vista), y **Resumen** sale del menú: lo abre el logo, que se marca cuando
  estás en él. Entra **Calendario**.
- **Una sola copia de la app por base de datos.** Al arrancar toma un candado
  (una fila en `settings` y `data/.scheduler-instance.json`, renovados cada
  30 s) y otra copia —en otra terminal, en otro puerto o en Docker— se niega a
  arrancar diciendo quién la tiene, en vez de publicar cada vídeo dos veces. Si
  el puerto está ocupado, se niega **antes** de arrancar el planificador (antes
  llegaba a publicar un momento hasta que uvicorn fallaba al abrir el puerto).
  Tras un corte, el candado caduca en 90 s (`INSTANCE_TAKEOVER=true` para no
  esperar). `run.sh` avisa también si el puerto está ocupado.
- **Las peticiones de otras webs no llegan a la app.** Cualquier página abierta
  en el navegador podía mandar un POST a 127.0.0.1:8787 («publicar ahora»,
  «entregar a Zernio»). Ahora lo que cambia algo tiene que venir de las páginas
  de la app (`Sec-Fetch-Site`/`Origin`) y toda petición, ir a uno de sus nombres
  (`Host`, contra el DNS rebinding; otros, en `ALLOWED_HOSTS`).
- **`run.sh` busca un Python 3.11 o superior** (`python3.13`, `3.12`, `3.11`, el
  de Homebrew) en vez de usar el `python3` que haya —en un Mac nuevo, el 3.9 de
  Xcode, con el que la app no arranca—, y rehace el entorno si se creó con uno
  viejo o con uno que ya no existe. `./run.sh --setup` prepara sin arrancar.
- **«Mover a…» abre un diálogo** con las colas en vez de pedir un número con
  `prompt()`.
- La demo (`tools/demo.sh`) guarda todo en `data/demo/` y trae el Ollama falso;
  el Zernio falso da URLs de subida con el nombre con el que se le llama (para
  la demo en Docker).
- **Los archivos ya no se mueven.** Publicar o fallar no los pasa a
  `media/posted` ni a `media/failed`: son de la biblioteca y otra cola puede
  necesitarlos. Quitar un vídeo de una cola, borrar una cola o «Limpiar» los
  fallidos nunca borra un archivo de la biblioteca; se borran desde la
  Biblioteca, y sólo cuando ninguna cola los necesita ya. «Borrar todo» sí
  vacía también `media/library`.
- **La carpeta vigilada es `media/library`** y no depende de ninguna cola: el
  interruptor «Recoger los vídeos de media/inbox» desaparece (esquema v8) y
  también el formulario de subida de la página Cola.

- **Modelo por defecto: un Gemma.** `OLLAMA_MODEL` vacío elige el más pequeño
  de 4 GB o más, Gemma 4 o Gemma 3 primero, y nunca uno de embeddings (antes
  elegía el más pequeño instalado, que con `embeddinggemma` era un modelo que no
  sabe escribir). Medido sobre 24 transcripciones y 12 fichas reales,
  `gemma4:26b` escribe mejor español que `qwen2.5:14b` en la mitad de tiempo
  (6–8 s por texto en un M2 Pro). Se le pide la respuesta con esquema JSON y con
  `think: false`, sin el cual Gemma 4 devuelve el JSON vacío.
- **Cinco hashtags como mucho y sin `#fyp`/`#parati`.** TikTok sólo cuenta los
  cinco primeros desde agosto de 2025, y los de alcance no dicen de qué va el
  vídeo. `HASHTAG_MAX` pasa de 6 a 5 (no se puede subir) y `HASHTAG_REACH=0`
  los deja fuera; ponlo a 1 o 2 si los quieres.
- **El modelo ve más contexto**: las últimas descripciones de la cuenta (para
  no abrir cincuenta vídeos de una serie igual), el nombre del archivo si dice
  algo y el nombre de la cola. Se descarga de memoria a los 2 minutos del
  último texto (`OLLAMA_KEEP_ALIVE`), para no competir con otros procesos.

### Corregido

- **«Recuperar de Zernio» en bloque ya no choca con el límite de peticiones**:
  recupera de 10 en 10 y espera 5 segundos entre tandas hasta terminar; si
  Zernio aun así responde 429, espera lo que pide y reintenta ese vídeo una vez.
  Antes paraba a mitad con «Too Many Requests» y había que volver a pulsarlo.
- **«Recuperar de Zernio» de un lote grande ya no bloquea la base de datos**:
  guarda cada vídeo en cuanto Zernio lo suelta, en vez de una sola transacción
  para todo el lote que retenía el bloqueo de escritura durante minutos. Con
  ella el latido de la app («no se pudo renovar el uso de la base de datos:
  database is locked»), el planificador y los textos esperaban 30 s y fallaban,
  la app entera se congelaba en cada espera, el planificador podía subir a
  Zernio un vídeo que el lote ya había recuperado, y si el proceso se cerraba a
  medias se perdían las recuperaciones que Zernio ya había hecho. El latido,
  además, va en su propio hilo: una base de datos ocupada ya no congela la app.
- **«Recuperar de Zernio» ya no se para en un vídeo que Zernio ya publicó**
  («Published posts cannot be deleted»: le llegó su hora antes de que el
  sondeo lo viera). Se marca como publicado, con su enlace y fechado por su
  hueco, y el lote sigue con los demás; el mensaje dice cuántos estaban ya
  publicados. Igual al recuperar uno solo, y al borrar una cola con «forzar»
  ya no cuenta como post que se quedó en Zernio.
- **«Añadir cuenta» se cerraba solo cada pocos segundos**, con la API key a
  medio pegar: cada refresco de la página ocultaba el formulario. Ahora sólo se
  oculta al pasar del alta de la primera cuenta a tener cuentas. De paso, las
  tarjetas de **Cuentas** ya no se redibujan mientras editas sus límites (antes
  se perdía lo escrito; ahora hay «Descartar»), y los desplegables de cuenta,
  carpeta y cola dejan de cerrarse solos cuando están abiertos.
- **Filtrar la Actividad por «Todas las cuentas»** después de elegir una
  fallaba: el refresco dejaba esa opción sin valor y se pedía
  `account_id=Todas las cuentas`.
- **En el móvil, el menú no cabía**: se quedaba en la primera fila, cortado por
  la derecha, en vez de bajar a la suya.
- **Con más de 500 vídeos en una cuenta, los que iban a salir antes
  desaparecían de la cola.** `/api/state` cargaba los 500 más recientes de cada
  cuenta —publicados incluidos— y calculaba los contadores sobre esa lista: los
  vídeos pendientes o en Zernio más antiguos, que son los primeros en salir,
  dejaban de verse, y «En cola», «En Zernio» o «Publicados» mentían. Ahora se
  lista siempre todo lo que está por salir y, del historial, los 300 más
  recientes (`STATE_HISTORY_LIMIT`, con el total real al lado); los contadores
  salen de la base de datos. Y cuesta lo mismo con 100 vídeos que con 4.000: las
  métricas y los comentarios ya no se piden vídeo a vídeo (con 1.000 vídeos,
  2.031 → 61 consultas y 234 → 83 ms por refresco), y las transcripciones no
  viajan en cada refresco (1,6 MB en vez de 3,2).
- **La demo registraba la biblioteca real.** `tools/demo.sh` usaba `media/library`
  —la carpeta vigilada de la app de verdad— y la carpeta de copias real.
- **Un vídeo recuperado o borrado mientras se subía a Zernio ya no deja un post
  huérfano allí.** La entrega guarda el vídeo antes de subirlo, así que
  «Recuperar de Zernio» o borrarlo (a él o a su cola) en ese momento lo quitaba
  de aquí, pero la subida terminaba y creaba igualmente el post en Zernio, que
  se habría publicado; recuperado, además, la cola local lo volvía a publicar.
  Al terminar la subida se comprueba que el vídeo sigue esperándola, y si no,
  el post recién creado se cancela y queda anotado en Actividad.
- **Se colaba chino y inglés en los textos.** De 200 textos escritos con
  `qwen2.5:7b`, 3 llevaban caracteres chinos (`#tiempo独自`, «un
  error社交失误，宇宙提醒你…») y 17 palabras en inglés («¿Lo needs?»,
  `#solitude`). El normalizador de hashtags aceptaba cualquier carácter
  alfanumérico, ideogramas incluidos, y nada revisaba el texto. Ahora un hashtag
  con otro alfabeto se descarta entero y un texto con él no se publica.
- **Hashtags fuera de tema.** Bastaba una palabra dicha de pasada para meter una
  categoría del banco: en una serie sobre la soledad salieron `#fitness` 39
  veces, `#hogar` 26, `#receta` 22 y `#tecnologia` 18, porque el vídeo decía
  «energía», «casa», «cena» o «móvil». Y «uñas» sin tilde es «unas», así que
  cualquier «unas semanas» era `#belleza`. Una categoría necesita ahora 3
  menciones de 2 palabras distintas, la ñ cuenta como letra propia, y el banco
  sólo completa cuando el modelo da menos de 3 hashtags.
- **Un vídeo delegado a Zernio antes de tener texto se subía sin él.** La
  entrega esperaba al texto, pero el bucle que lo escribe no miraba los vídeos
  delegados: esperaba diez minutos para nada. Ahora los escribe mientras no
  estén subidos, y «Regenerar» también funciona con ellos.

## 3.3 · las colas funcionan a la vez (17 de septiembre de 2026)

### Corregido

- **Una segunda cola ya no espera a que termine la primera.** La separación
  mínima de la cuenta se aplicaba adelantando el «ahora» hasta la **última**
  hora que la cuenta tuviera reservada, así que bastaba con que una cola
  tuviera dos semanas programadas —o entregadas a Zernio— para que la siguiente
  no encontrara hueco hasta el final de aquélla: se creaba una cola nueva, se
  subían diez vídeos y el primero salía para dentro de dos semanas.

  Ahora la separación es lo que siempre debió ser: una **distancia a las
  publicaciones vecinas**, no un turno de espera. Cada cola planifica esquivando
  las horas que la cuenta ya tiene ocupadas, de modo que **las colas corren a la
  vez** y se intercalan. Una cola nueva coloca su primer vídeo hoy, en los
  huecos que la otra dejó libres, sin acercarse a ninguno más de lo permitido.
- **Reparto justo entre colas al publicar.** Cuando varias colas tienen el hueco
  vencido en el mismo ciclo sale la del **hueco más antiguo**, no la primera de
  la lista; el orden sólo desempata. Antes, una primera cola siempre cargada
  podía dejar a las demás sin publicar nunca.
- **Un hueco reservado bajo reglas viejas vuelve a su sitio.** El primer vídeo
  de una cola sólo conserva una hora posterior a la calculada si es un
  **reintento** (que lleva su propio plazo). Cualquier otro se recoloca en cuanto
  se libera una hora mejor, en vez de quedarse clavado donde lo dejó una
  configuración anterior. Al arrancar, si la base de datos se ha migrado, todas
  las colas se replanifican una vez: lo que quedó fijado con las reglas de la
  versión anterior no sobrevive a la actualización.
- **La previsualización enseña la hora que de verdad está fijada.** El primer
  vídeo de la cola dejó de contarse como un obstáculo para sí mismo, que
  desplazaba la simulación una separación entera.

- **Las analíticas nunca llegaban: se leía un campo que ese endpoint no manda.**
  `GET /analytics` identifica cada post con `latePostId` (el id del post que
  creó Zernio, que es el que esta herramienta guarda) y `postId` (el id del
  vídeo en TikTok). El parser buscaba `_id`, como en `GET /posts/{id}`, no lo
  encontraba en ninguna fila y las descartaba todas en silencio: la consulta
  respondía «0 vídeo(s)» una y otra vez, sin ningún error, y la tabla de
  métricas seguía vacía indefinidamente. Ahora se lee el id correcto y se casa
  por cualquiera de los dos, así que un vídeo aparece aunque Zernio lo nombre
  con el id de la plataforma.

  No lo cazaron las pruebas porque el Zernio falso devolvía `_id`, el mismo
  campo equivocado: estaba comprobando la suposición en vez del contrato. El
  mock responde ahora con la forma documentada, y hay una prueba unitaria sobre
  ese payload exacto para que un cambio de nombre se vea aquí y no en
  producción.
- **Un vídeo del que TikTok aún no ha informado ya no se guarda como ceros.**
  Zernio lo marca `syncStatus: pending`; guardarlo como cero reproducciones
  falsearía las medias por hora y borraría lo que ya hubiera. Se deja para la
  siguiente consulta.
- **La hora de publicación de un vídeo entregado a Zernio ya no es la hora en
  que la app se entera.** Zernio publica por su cuenta, puede que con el
  ordenador apagado, y la herramienta sólo lo descubre al volver a abrirse: toda
  la tanda se quedaba con el segundo exacto del sondeo. En la base de datos de
  ejemplo, tres vídeos programados a las 05:01, 06:11 y 07:14 figuraban los tres
  publicados a las 07:41:42. Las analíticas de **mejor hora** y **mejor día** se
  calculan sobre ese campo, así que atribuían las visualizaciones a la hora en
  que se abrió la aplicación.

  Ahora se usa el `publishedAt` de Zernio cuando lo manda y, cuando no —hoy su
  `GET /posts/{id}` no lo trae, sólo lo llevan los webhooks—, la hora a la que
  se le encargó publicar. Un vídeo que esta aplicación sí ve publicarse se sigue
  fechando con el reloj, que ahí es exacto. Esquema **v7**: una migración
  devuelve su hora a las filas ya guardadas que llevan la marca del sondeo
  —entregadas, muy posteriores a su hora y compartiendo instante con otra
  publicación de la misma cuenta—; una publicación real, aunque llegara unos
  minutos tarde, no se toca.

### Ajustes

- **La separación mínima es una sola y está en la cuenta.** Las colas tenían
  además la suya, invisible en la interfaz, que hacía de suelo del intervalo
  aleatorio; con dos números llamados igual, el de la cola podía quedar por
  debajo del de la cuenta. Ahora `Separación mínima` hace los dos trabajos:
  separa dos vídeos seguidos de una misma cola y separa dos colas distintas.
- **Los límites de la cuenta se editan en Cuentas, no en Programación.** El
  máximo diario y la separación mínima valen para todas las colas, así que no
  pintaban nada en una pantalla que configura la cola que tengas abierta —de
  ahí el «0 ·» con el que había que colarlos antes del resto—. Pasan a la
  tarjeta de la cuenta, donde además ya se mostraban en sólo lectura, y
  Programación se queda con una línea que los recuerda, los avisos de cuando
  las colas suman más de lo que la cuenta admite y un enlace para cambiarlos.
- Esquema **v6**: `schedules.min_gap_minutes` desaparece. Cada cuenta se queda
  con **la mayor** de las separaciones que tuviera —la suya y las de sus
  colas—, así que ninguna instalación sale de la actualización publicando más
  apretado de lo que publicaba.

## 3.2 · la cola entera a Zernio, y la IA marcada sola (16 de septiembre de 2026)

### Nuevo

- **Mandar la cola entera a Zernio.** Un botón en **Cola** calcula la hora de
  cada vídeo pendiente con las reglas de esa cola (intervalo, franjas, días,
  máximo diario propio y techo de la cuenta), enseña el plan completo —días que
  ocupa, separación entre vídeos, hora exacta de cada uno— y, al confirmar, se
  los entrega a Zernio ya programados (`scheduledFor` + `timezone`). A partir de
  ahí publica Zernio: **se puede cerrar la aplicación**. Los vídeos salen de la
  rotación local en el momento de enviarlos y se suben de uno en uno, con la
  barra de progreso en la propia página; una entrega interrumpida se reanuda
  sola al volver a arrancar.
- **Estado nuevo «En Zernio»** (`DELEGATED`) con su filtro en la cola, su línea
  en las próximas publicaciones del Resumen y sus contadores. Cuenta para el
  máximo diario: es una publicación que va a ocurrir.
- **Deshacer la entrega**: **Recuperar de Zernio** (toda la cola) o
  **Recuperar** (un vídeo), que cancelan el post en Zernio antes de devolverlo a
  la cola. **Quitar** un vídeo entregado también lo cancela allí primero. Nada
  puede dejar un vídeo publicándose por su cuenta sin que quede constancia aquí:
  «Ahora» rechaza un vídeo entregado, «Reintentar» uno que falló en Zernio lo
  cancela antes de volver a la cola, y una cola con vídeos entregados sólo se
  puede eliminar moviéndolos a otra.
- **Etiqueta de IA automática.** «Contenido generado por IA» pasa de ser una
  casilla por cola que había que acordarse de marcar a ser **el valor por
  defecto con el que nace cada vídeo**: la cola lo decide al entrar, el vídeo se
  lo queda y el botón **🤖 IA** de la cola lo apaga o lo enciende vídeo a vídeo.
  Cambiar la cola ya no reescribe lo que espera en ella.
- **Borrar todo** en **Base de datos**: vacía todas las tablas y borra los
  `.mp4`, `.mov` y `.webm` (con sus textos) de `media/inbox`, `media/posted` y
  `media/failed` — y sólo de ahí. Exige **tres comprobaciones seguidas**
  (una suma y una palabra, generadas y verificadas en el servidor); fallar una
  cancela el borrado entero. Antes de borrar guarda una copia de la base de
  datos en `data/backups`, avisa de los vídeos que sigan programados en Zernio
  (borrar aquí no los cancela) y se niega mientras haya una subida en marcha.

### Corregido

- Los filtros de la cola (Pendientes, Publicados…) no se aplicaban hasta la
  siguiente recarga automática: el re-render fallaba por dentro.
- **La subida sobrevive a los bloqueos de rangos de Cloudflare.** Los bytes no
  van a `zernio.com` sino a Cloudflare R2, en otro rango de direcciones: cuando
  ese rango está bloqueado, la API responde con normalidad y sólo la subida se
  queda colgada 75 s antes de fallar, vídeo tras vídeo. Ahora el intento se
  corta a los 15 s y se repite por la dirección de `media.zernio.com` (mismo
  borde de Cloudflare, que reparte por nombre de dominio), conservando Host y
  SNI del destino original —la firma de la URL temporal cubre Host y el
  certificado se valida contra el nombre—, y la ruta que funcionó se reutiliza
  15 minutos para el resto de la cola. Si tampoco hay ruta, el mensaje lo
  explica en vez de dejar un «fallo no clasificado» con su traza.

### Ajustes

- `.env`: `DELEGATE_BATCH_PER_TICK`, `DELEGATED_POLL_MINUTES` y
  `DELEGATED_TIMEOUT_HOURS` (48 h; Zernio retiene los posts que superan el cupo
  de 15/día y los suelta más tarde, así que la espera es larga a propósito).
- Esquema **v5**: `video_items.is_aigc` y `video_items.delegated_at`; la
  migración enciende la etiqueta de IA en las colas que ya existían y se la
  copia a los vídeos que siguen esperando. Lo ya publicado no se toca.

---

## 3.1 · el vídeo escribe su texto (16 de septiembre de 2026)

### Nuevo

- **Textos automáticos.** Un vídeo que entra en la cola sin texto se transcribe
  en local con **Whisper** (faster-whisper, pesos en `data/models/whisper/`) y,
  si hay un **Ollama** local, un modelo de lenguaje convierte la transcripción en
  un gancho de 150 caracteres; sin Ollama, el texto es la primera frase del vídeo.
  Se genera **al entrar en la cola**, en un bucle aparte del que publica, de uno
  en uno; la cola muestra en qué punto está cada vídeo (en cola · escuchando ·
  generado · fallido) y cuántos textos van en camino. Un vídeo cuyo texto no está
  listo espera hasta `CAPTION_MAX_WAIT_MINUTES` y luego sale con lo que tenga.
  Lo que escribas a mano siempre gana. Ver [docs/CAPTIONS.md](docs/CAPTIONS.md).
- **Hashtags automáticos**, por vídeo: los fijos de Ajustes primero, luego los
  del tema (propuestos por Ollama o extraídos de la transcripción, más los del
  banco `data/hashtags.json`, que detecta la categoría del vídeo) y por último
  los de alcance; máximo `HASHTAG_MAX`. Se normalizan (minúsculas, sin símbolos,
  acentos y ñ conservados, `#hábitos` = `#habitos`) y se **ordenan por lo que ya
  funcionó en tu cuenta**: un hashtag con tres o más vídeos publicados se puntúa
  por su media de reproducciones y los flojos se caen de la lista.
- Botones **Etiquetas** y **Regenerar** (o **Escribir solo**) en cada vídeo de
  la cola; **Analíticas → Qué hashtags funcionan**; estado de Whisper y Ollama en
  Ajustes → Sistema.
- `run.sh` instala las dependencias nuevas cuando `requirements.txt` cambia (antes
  sólo lo hacía al crear el entorno).

### Ajustes

- `.env`: bloque nuevo `CAPTION_AI_ENABLED`, `WHISPER_*`, `OLLAMA_*`,
  `HASHTAG_MAX`, `CAPTION_MAX_WAIT_MINUTES`. Dependencia nueva: `faster-whisper`.
- Columnas nuevas en `video_items` (`caption_status`, `caption_source`,
  `caption_error`, `caption_generated_at`, `caption_requested_at`, `transcript`,
  `auto_hashtags`); se añaden solas al arrancar, con su índice.

---

## 3.0 · una sola forma de publicar, y el después (16 de septiembre de 2026)

Dos cambios en direcciones opuestas: se va todo lo que servía para *no* publicar
directamente, y llega lo que pasa **después** de publicar. Zernio ya ofrecía
analíticas y buzón de comentarios en el plan gratuito; no se estaban usando.

### Nuevo

- **Analíticas** por vídeo: reproducciones, me gusta, comentarios, compartidos y
  % de interacción, más vistas de perfil y % de vídeo visto entero en las cuentas
  conectadas por el carril de Business. La pestaña muestra los totales, el
  ranking de mejores vídeos y la **media por hora y por día de la semana**, para
  saber si las franjas configuradas son las buenas. Se actualiza sola cada 3 h.
  Ver [docs/ANALYTICS.md](docs/ANALYTICS.md).
- **Comentarios**: los de los vídeos publicados en los últimos 14 días, con
  **responder** y **ocultar / volver a mostrar** desde la propia herramienta, y
  un contador de pendientes en el menú. Se consultan cada 30 minutos (no hay
  webhooks: esto corre en `127.0.0.1` sin URL pública). Ver
  [docs/COMMENTS.md](docs/COMMENTS.md).
- **La cola muestra los números** de cada vídeo ya publicado, en su propia línea.
- Ajustes nuevos en `.env`: `ANALYTICS_REFRESH_MINUTES`, `ANALYTICS_WINDOW_DAYS`,
  `COMMENTS_ENABLED`, `COMMENTS_POLL_MINUTES`, `COMMENTS_WINDOW_DAYS`,
  `COMMENTS_BATCH`. Poner los dos primeros a 0, o `COMMENTS_ENABLED=false`,
  apaga cada cosa por separado.

### Eliminado

- **Borradores de TikTok.** El envío a la bandeja del creador y el interruptor
  *Enviar a borradores* desaparecen. Todo se publica directo.
- **Selector de privacidad.** El carril de TikTok for Business por el que
  conecta Zernio sólo publica vídeos públicos; fingir lo contrario obligaba a
  desviar a borradores. Ahora `PUBLIC_TO_EVERYONE` es una constante.
- **Sound Boost**, la pestaña **Sonidos**, la tabla `sounds`, el archivo
  `data/trending_sounds.json` y el proveedor *Creative Center*. No existe ninguna
  API pública de TikTok que adjunte un sonido del catálogo viral, así que la
  función sólo podía sugerirte uno y mandarte a la app a ponerlo a mano.
  `docs/SOUND_BOOST.md` se retira; el porqué resumido queda en la FAQ.
- Con ellos se va la regla «el contenido de marca no puede ser privado»: sin
  privacidad que elegir, no hay nada que validar.

### Migración

- **Esquema v4**, automático al arrancar: se eliminan las columnas
  `schedules.to_drafts`, `schedules.privacy_level`, `schedules.sound_boost_enabled`
  y `video_items.suggested_sound`, y la tabla `sounds`. Se crean `video_metrics`
  y `comments`. **Ningún vídeo ni ningún registro de actividad se pierde**: sólo
  desaparecen columnas. Haz una copia igualmente (Ajustes → Crear copia ahora).
- Las claves `SOUNDS_*` que queden en tu `.env` se ignoran.

---

## 2.1 · varias colas por cuenta (septiembre de 2026)

Una cuenta de TikTok se sigue conectando una sola vez, pero ahora puede tener
varias **colas**: listas de vídeos independientes, cada una con sus horas y su
ritmo. El caso que lo motivó: publicar de 10:00 a 22:00 con una cola y de 22:00
a 10:00 con otra, sin duplicar la cuenta ni la clave de Zernio.

### Nuevo

- **Colas por cuenta**: crear, renombrar, **duplicar** (copia las reglas, no los
  vídeos), reordenar y eliminar, desde la barra de pestañas de **Cola** y
  **Programación**, desde el **Resumen** o desde **Cuentas**. Cada cola tiene su
  propio interruptor, intervalo, máximo diario, franjas, días, privacidad y modo
  de publicación.
- **Límites de la cuenta** (Programación → 0): máximo de vídeos al día que
  comparten todas sus colas —15 por defecto, lo que admite Zernio— y separación
  mínima entre dos publicaciones, vengan de la cola que vengan. El planificador
  los aplica al calcular cada hueco, y la interfaz avisa si las colas suman más
  de lo que la cuenta permite.
- **Mover a…** ahora manda un vídeo a cualquier cola, de la misma cuenta o de
  otra. El **Resumen** y la vista de **Cuentas** listan las colas una a una con
  su franja y su próxima publicación.
- **Eliminar una cola** abre un diálogo que dice qué se va y qué se queda, y
  obliga a elegir si tiene vídeos: moverlos a otra cola, o borrar la cola con
  ellos (y opcionalmente con sus archivos del disco). El registro de Actividad
  se conserva siempre.
- **Migración v3 automática**: el índice de `schedules.account_id` deja de ser
  único y cada vídeo queda atado a su cola (`video_items.schedule_id`). Las
  instalaciones existentes se quedan con una cola llamada «Cola principal» y sus
  vídeos dentro; no hay nada que hacer a mano.

### Cambios

- El planificador recorre las colas de cada cuenta en orden y publica **un vídeo
  por ciclo y cuenta**: dos colas de la misma cuenta nunca suben a la vez.
  Después de publicar, recalcula el hueco de todas las colas de esa cuenta.
- La carpeta vigilada la reclama una **cola**, no una cuenta. Las colas nuevas
  nacen sin ella para no competir con la que ya la tenía.
- La API pasa a ser por cola: `PUT /api/schedules/{id}`,
  `POST /api/schedules/{id}/videos`, `/scan`, `/preview`, y
  `POST /api/accounts/{id}/schedules` para crear. `PATCH /api/videos/{id}` mueve
  con `schedule_id` en vez de `account_id`.

## 2.0 · sólo Zernio (septiembre de 2026)

Limpieza general: desaparecen la API oficial de TikTok (OAuth, auditoría, HTTPS
local), el modo manual/asistido y el webhook genérico. Publicar es una sola cosa:
a través de Zernio. Menos de la mitad de código, y una interfaz de administración.

### Nuevo

- **Menú superior** con Resumen, Cola, Programación, Sonidos, Actividad, Cuentas,
  Base de datos y Ajustes, con rutas propias (`#/cuentas`, `#/db/video_items`…)
  y selector de cuenta arriba a la derecha.
- **Resumen**: todas las cuentas de un vistazo, próximas publicaciones de todas
  ellas, última actividad, activar/parar programación con un clic.
- **Cuentas**: conectar varias cuentas de TikTok a la vez con una misma API key,
  probar la conexión, **mostrar** o **sustituir** la API key de Zernio
  (registrado en Actividad), renombrar, **activar/desactivar** y eliminar.
- **Base de datos**: navegador tipo phpMyAdmin con tablas, paginación,
  ordenación, búsqueda, edición y borrado de filas (secretos enmascarados), consola
  SQL de sólo lectura y exportación CSV.
- **Ajustes**: copias de seguridad de la base de datos (crear, descargar,
  borrar), mantenimiento (recortar el registro, limpiar fallidos) y panel de
  sistema (versiones, planificador, tamaños de carpetas, vídeos por estado).
- **Actividad** con filtros por nivel, cuenta y texto.
- **Cola** con filtros (pendientes, publicados, fallidos) y **Mover a…** otra cuenta.
- **Programación**: interruptor *Enviar a borradores de TikTok* por cuenta.
- **Migración v2 automática**: al arrancar, las cuentas antiguas (manual, OAuth)
  se eliminan y sus vídeos pasan a la primera cuenta de Zernio sin duplicados;
  las columnas obsoletas se renombran o se borran. Requiere SQLite ≥ 3.35.
- `tools/mock_zernio.py` y `tools/demo.sh` para probarlo todo sin cuenta.

### Eliminado

- `app/tiktok/` (OAuth, Content Posting API, troceado), `app/tls.py`, el modo
  manual, el webhook genérico, `site/` y la documentación de creación y auditoría
  de la app de TikTok. Se conserva una copia del proyecto anterior en
  `data/backups/pre-cleanup-*.tar.gz`.

## 1.x · revisión de septiembre de 2026

- Modo Relay (Zernio + webhook), interfaz conforme a las guías UX de TikTok,
  avisos multi-servicio (Discord, Slack, ntfy, Telegram), modo asistido, migración
  automática de columnas.
- Corregido: el webhook de Ajustes no se usaba; la carpeta vigilada duplicaba
  vídeos entre cuentas; los borradores se marcaban como fallidos a los 60 min;
  las subidas por interfaz cargaban el archivo entero en RAM.
