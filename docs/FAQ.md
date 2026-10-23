# Preguntas frecuentes

## Zernio

### ¿Qué es Zernio y por qué hace falta?

Un servicio que ya tiene su propia app de TikTok auditada. TikTok sólo deja
publicar en público a las apps auditadas, y auditar la tuya exige web pública,
vídeos de demostración y semanas de revisión. Conectas tu TikTok a Zernio, pegas
su API key aquí y la cola se publica sola, en público. Gratis hasta dos cuentas
→ [ZERNIO.md](ZERNIO.md).

### ¿Qué le doy a Zernio exactamente?

Acceso a publicar en tu TikTok (lo autorizas tú en la web de TikTok) y, en cada
publicación, el vídeo, el texto y la configuración (privacidad, interacciones,
declaraciones). Tu contraseña de TikTok no pasa ni por Zernio ni por esta
herramienta.

### ¿Dónde guarda la herramienta la API key?

Cifrada con Fernet en `data/scheduler.db`, con la clave de
`.secrets/encryption.key`. La API local nunca la devuelve; sólo **Cuentas →
Mostrar clave**, y cada consulta queda en Actividad.

### He perdido la API key

**Cuentas → Mostrar clave**. Si además la revocaste en Zernio, crea otra allí y
**Sustituir clave**.

### ¿Puedo publicar en privado o «sólo seguidores»?

No. Zernio publica a través de la app de TikTok for Business, que sólo admite
vídeos **públicos**. Hasta la v2 la herramienta rodeaba eso enviando el vídeo a
tus borradores de TikTok; en la v3 se quitó, porque en la práctica nadie quería
publicar a mano lo que había programado. Todo sale público y directo.

### ¿Puede Zernio hacer algo más aparte de publicar?

Sí, y la herramienta usa dos cosas más: **analíticas** por vídeo y el **buzón de
comentarios**. Ambas están incluidas en el plan gratuito. Ver
[ANALYTICS.md](ANALYTICS.md) y [COMMENTS.md](COMMENTS.md).

Lo que **no** existe en ninguna API pública de TikTok, y por tanto tampoco en
Zernio: seguir o dejar de seguir cuentas (o sea, nada de *follow-back*
automático), listar tus seguidores, dar «me gusta» a contenido ajeno, crear dúos
o stitches, hacer directos, editar un post ya publicado, o adjuntar un sonido del
catálogo viral. Las herramientas que prometen follow-back automatizan la app por
debajo, lo cual va contra los términos de TikTok y suele acabar en shadowban.

### ¿Y los mensajes directos?

No, y no por esta herramienta: TikTok exige que la cuenta sea Business y que su
región de registro esté **fuera del EEE, Suiza y Reino Unido**. Una cuenta
española no cumple la segunda condición. Además sólo se podría responder, nunca
iniciar una conversación.

### ¿Cuántos vídeos puedo publicar al día por Zernio?

15 por cuenta cada 24 horas (límite de Zernio). Es el valor por defecto del
**máximo de la cuenta** en Programación, y lo comparten todas sus colas. Con los
presets de intervalo no llegas ni de lejos.

### ¿Zernio guarda mis vídeos?

Los sube a un almacenamiento temporal para entregarlos a TikTok; su documentación
habla de unos 7 días. Para lo que hacen con tus datos, lee su política de
privacidad: es un tercero y esta herramienta no puede responder por él.

### ¿Qué pasa si dejo de pagar o borro la cuenta de Zernio?

Los vídeos que aún no hayan salido fallarán con un error claro (`401`/`402`) y no
se reintentan. Los que ya se publicaron siguen en tu TikTok: son posts normales.

### ¿Puedo tener varias cuentas de TikTok?

Sí. Cada una es una fila en **Cuentas**, con sus colas, sus reglas y su copia de
la clave. Las dos primeras son gratis en Zernio; a partir de la tercera, 6 $/mes
cada una. El **Resumen** (el logo) las muestra todas —o las de una plataforma—,
el **Calendario** pone la semana de todas en carriles y el selector de arriba a
la derecha cambia entre ellas en Cola y Programación.

### ¿Puedo conectar la misma cuenta dos veces para tener dos colas?

No hace falta, y no se puede: una cuenta de TikTok se conecta una sola vez. Lo
que se multiplica son las **colas** dentro de ella. En **Programación → + Nueva
cola** (o **Duplicar**, que copia las reglas de la actual) añades una segunda
lista de vídeos con su propio horario: por ejemplo «Día» de 10:00 a 22:00 y
«Noche» de 22:00 a 10:00. Misma clave de Zernio, misma cuenta de TikTok, sin
pagar otra cuenta.

Cada cola se activa y se para por separado y tiene su propio intervalo, su
máximo diario, sus franjas, sus días y sus opciones de interacción. Lo que
comparten: el máximo diario de la cuenta, la separación mínima entre
publicaciones y el hecho de que sólo sube un vídeo cada vez.

### Con dos colas, ¿publican a la vez o una detrás de otra?

**A la vez.** Las dos van colocando vídeos en los mismos días, cada una según sus
franjas y su intervalo; una cola nueva empieza hoy aunque la otra tenga semanas
llenas por delante (incluso entregadas a Zernio). Lo único que las separa es la
**separación mínima de la cuenta**: cada hueco nuevo se aparta de las
publicaciones vecinas, por lo que las colas se intercalan en vez de solaparse.

A la hora de subir sí hay turno: sólo sale **un vídeo por ciclo y cuenta**, y de
las colas con el hueco vencido va la del hueco más antiguo. Es cuestión de
segundos, no de días.

Si de verdad quieres que una cola espere a la otra, dales franjas que no se
solapen (10:00–22:00 y 22:00–10:00, por ejemplo).

### Si pongo 5 al día en cada cola, ¿publica 10?

Sólo si el máximo de la cuenta lo permite. Las colas se reparten un único
presupuesto diario: con el tope de la cuenta en 6, saldrán 6 y las últimas colas
del día se quedarán sin hueco. La pantalla de Programación lo avisa cuando la
suma de las colas supera el tope.

---

## Uso general

### ¿Tengo que dejar el ordenador encendido?

Depende de quién publique. Si publica la app, sí: es local, no un servicio en la
nube, y si el proceso no corre no publica (al volver a arrancarla retoma la cola
y recalcula el siguiente hueco). Para 24/7 así, un mini-PC, un NAS o un VPS
pequeño con Docker bastan (`docker compose up -d`; ver
[INSTALL.md](INSTALL.md#en-un-vps-o-un-equipo-siempre-encendido)).

Si mandas la cola a Zernio (**Cola → Mandar la cola entera a Zernio**), no: cada
vídeo queda allí con su hora y Zernio publica aunque el ordenador esté apagado.
Lo único que hay que esperar es a que terminen de subirse, que va de uno en uno.

### ¿Puedo usarlo en Docker? ¿Y en un Mac nuevo?

Sí a las dos. En un Mac nuevo, `make bootstrap` instala lo que hace falta y
`make doctor` dice si falta algo; para llevarte tu instalación, `make snapshot`
en el viejo y `make restore` en el nuevo. Docker (`make docker-up`) comparte los
mismos datos que la instalación nativa y usa el Ollama del Mac; en el propio Mac
lo recomendable sigue siendo nativo (Docker no ve la GPU), y Docker brilla en un
equipo siempre encendido. Nunca los dos a la vez: el segundo se niega a arrancar.
Todo en [INSTALL.md](INSTALL.md).

### ¿Cuándo conviene mandar la cola a Zernio y cuándo no?

Mándala cuando vayas a apagar el equipo o a estar días fuera y ya tengas los
vídeos y sus textos como quieres. Déjala aquí cuando estés trabajando sobre ella:
mientras los vídeos están en Zernio no puedes cambiarles el texto, los hashtags
ni la hora sin recuperarlos antes (**Recuperar de Zernio**, que los cancela allí
y los devuelve a la cola).

Lo que no cambia en ninguno de los dos casos: las horas. Se calculan con las
mismas reglas de la cola, así que un vídeo sale a la misma hora lo publique quien
lo publique.

### He mandado la cola a Zernio y quiero cambiar un vídeo

**Cola → filtro En Zernio → Recuperar** en ese vídeo. Se cancela en Zernio y
vuelve a la cola local; cámbialo y vuelve a enviar la cola cuando quieras. Un
vídeo que ya se publicó no se puede recuperar: ninguna API de TikTok despublica.

### ¿Qué pasa si cierro la app a media subida?

El vídeo se queda en estado *procesando*. Al reabrir, el planificador consulta su
estado en Zernio y lo resuelve. Lo mismo vale para una entrega a medias: los
vídeos que ya subieron se quedan programados en Zernio y el resto siguen
subiendo en cuanto vuelvas a arrancar.

### Tengo varias colas. ¿A cuál van los vídeos que subo?

A ninguna todavía: todo entra en la **Biblioteca**. Desde cada cola, «Añadir
desde la biblioteca…» elige qué vídeos entran en ella (o una carpeta entera).
El mismo vídeo puede estar en una cola de TikTok y en otra de YouTube, y ocupa
el disco una sola vez.

### ¿Puedo mover un vídeo de una cola a otra?

Sí: **Mover a…** en cada vídeo pendiente abre la lista de colas. Sirve tanto
entre colas de la misma cuenta como entre cuentas distintas. El mismo archivo no puede estar dos veces en
la misma cuenta, ni siquiera en colas diferentes.

### Si elimino una cola, ¿pierdo sus vídeos?

Tú decides, en el diálogo que sale al pulsar **Eliminar cola**:

- **Mover sus vídeos a otra cola**: no se pierde nada, ni los que esperan ni el
  historial de lo publicado.
- **Borrar la cola con sus vídeos**: se van también sus filas del historial. Los
  archivos se quedan en `media/` salvo que marques *borrar también los archivos
  del disco*.

Si la cola tiene vídeos hay que elegir una de las dos: no se borra nada por
omisión. El registro de **Actividad** no se toca en ningún caso, así que siempre
queda constancia de lo que publicó. La última cola de una cuenta no se puede
eliminar (para eso está **Eliminar** la cuenta entera), ni tampoco una que esté
subiendo un vídeo en ese momento.

Si tiene vídeos entregados a Zernio, el diálogo te avisa y, para borrarla con
ellos, marcas **Borrarla igualmente**. Antes de borrar se intenta cancelarlos en
Zernio; si ya los habías borrado allí no pasa nada. Los que Zernio no deje
cancelar se publicarán aunque desaparezcan de aquí, y el aviso te dice cuántos.

### He vaciado la cola en Zernio a mano y la app sigue creyendo que están allí

La app no se entera de lo que borras en Zernio hasta que llega la hora de cada
vídeo (sólo pregunta por un post cuando ya debería haber salido). Mientras
tanto, esos vídeos siguen ocupando sus horas: los nuevos de la misma cola se
ponen detrás del último, y las demás colas los esquivan y los cuentan en el
máximo diario de la cuenta. Para liberarlas:

- **Recuperar de Zernio** si quieres conservar los vídeos: la cancelación
  responde 404 y se da por buena, y vuelven a la cola local (que los publicará
  cuando toque, salvo que la desactives o los borres).
- **Eliminar cola → Borrar la cola con sus vídeos → Borrarla igualmente** si no
  los quieres.

Pausa la app antes si aún quedaban vídeos de la tanda por subir: el worker los
seguiría mandando a Zernio uno a uno.

### ¿Los vídeos se borran de mi disco?

No. Se mueven: a `media/posted/` si se publican, a `media/failed/` si fallan
definitivamente. Sólo se eliminan si tú lo pides (**Ajustes → Mantenimiento →
Vídeos fallidos**, con la casilla de borrar archivos).

### ¿Se puede cambiar la configuración con la cola en marcha?

Sí. Al guardar, los huecos pendientes se recalculan con las reglas nuevas. Un
vídeo que ya esté subiendo termina su subida con las reglas viejas.

### ¿Puedo ver el vídeo antes de que salga?

Sí: botón **Ver** en cada vídeo de la cola. Y **Texto** para editar su pie.

### ¿Para qué sirve desactivar una cuenta?

Para pararla del todo sin tocar su programación: ninguna de sus colas publica ni
recoge vídeos. Cuando la reactivas, sigue donde estaba. Para parar sólo una cola,
usa su interruptor de **Programación activa en esta cola**.

---

## Programación

### ¿Cómo se elige el momento exacto dentro del rango?

Se sortea un número aleatorio uniforme entre el mínimo y el máximo, y encima se
suma un *jitter* de unos segundos. Con un rango de 25–35 min, dos publicaciones
seguidas nunca llevan la misma separación.

### ¿Qué significa "indiferente" en la franja horaria?

Que no hay restricción de hora: puede publicar a las 04:00 igual que a las 14:00.
Es el valor por defecto y, con él, lo único que limita es el máximo diario.

### Tengo franja 22:00–06:00 y máximo 5. ¿Cómo se cuenta?

El máximo es **por día natural**. En una franja nocturna eso significa que el
contador se reinicia a medianoche, en mitad de la franja. Si quieres controlar el
total de una noche, usa el intervalo en lugar del máximo diario: con 55–75 min,
una franja de 8 horas da 6–8 vídeos por sí sola.

### Puse máximo 20 al día pero sólo publica 6

Manda el intervalo, no el máximo. Con un intervalo de 150–210 minutos no caben 20
vídeos en un día por mucho que subas el tope. La herramienta te avisa al guardar.

### ¿Qué pasa en los cambios de hora?

Está contemplado. El cálculo trabaja con instantes reales, así que no se salta
huecos ni los duplica. Hay tests específicos para ambos casos.

### ¿Publica aunque la cola esté vacía?

No. Si no hay vídeos, el hueco pasa sin más. En cuanto añades uno, se le asigna
el siguiente hueco válido.

### ¿Y si añado un vídeo justo después de una publicación?

Espera al siguiente hueco normal. Si quieres saltarte la cola, usa **Ahora**:
publica en unos segundos, y la cadencia se reinicia desde esa publicación (así
nunca salen dos vídeos más juntos de lo que permite tu intervalo).

---

## Publicación

### ¿Cómo se publica exactamente?

Directa y pública, en cuanto toca el hueco, sin que hagas nada. No hay otra
opción: es lo único que admite el carril de TikTok for Business por el que
conecta Zernio. Lo que sí eliges por cola: interacciones (comentarios, dúos,
stitch) y declaración de contenido comercial; la etiqueta de IA la lleva cada
vídeo.

### ¿Cuánto puede durar un vídeo?

De 3 segundos a 10 minutos, hasta 4 GB.

### ¿Qué formatos acepta?

MP4 (recomendado), MOV y WebM. Lo ideal: **MP4, H.264 + AAC, 1080x1920, 30 fps**.

### ¿Qué comprueba antes de subir?

Duración, resolución (360–4096 px por lado), FPS (23–60), códec, presencia de
audio, tamaño y relación de aspecto. Los problemas graves bloquean la subida; los
menores (vídeo horizontal, sin audio, bitrate bajo) sólo avisan.

### ¿Puedo poner un texto distinto a cada vídeo?

Sí: con un `.txt` del mismo nombre junto al vídeo al subirlo a la biblioteca
(`clip.mp4` + `clip.txt`), con **Textos** en la Biblioteca (para todas sus colas,
por plataforma) o con el botón **Texto** en la cola (sólo para esa cola).

### ¿Para qué sirve `{a|b|c}` en el texto?

Para que no se repita el mismo pie. `{Mira esto|No te lo pierdas|Atención}` elige
uno al azar en cada publicación. Repetir un texto idéntico decenas de veces es la
señal de bot más obvia que existe.

### ¿Por qué mis vídeos salen marcados como generados por IA?

Porque es el valor por defecto de la cola: cada vídeo que entra nace con la
etiqueta de TikTok «contenido generado por IA» puesta, que es lo que TikTok pide
para el contenido generado o modificado de forma realista con IA. La decisión
viaja con el vídeo, no con la cola: cambiar la cola más tarde no reetiqueta lo
que ya está esperando dentro.

Para quitársela a uno suelto, el botón **🤖 IA** en la cola (encendido = se
declara). Para que los próximos entren sin ella, **Programación → 5 · Cómo se
publica → Marcar el contenido como generado por IA**.

Ojo con marcar de más y de menos: no declarar contenido realista hecho con IA va
contra las normas de TikTok, y declarar como IA un vídeo que no lo es suele
costar alcance.

### ¿La etiqueta de IA tiene que ver con los textos automáticos?

No. La etiqueta habla del **vídeo**: que las imágenes o el audio sean generados o
retocados con IA. Que el pie lo haya escrito Whisper + Ollama no convierte un
vídeo real en contenido de IA, y por eso los textos automáticos no encienden ni
apagan la etiqueta.

### ¿Qué es «Declarar contenido comercial»?

La etiqueta que TikTok exige cuando un vídeo promociona tu propia marca
(«Contenido promocional») o una colaboración pagada («Colaboración pagada»). Si
no aplica, déjalo apagado. El contenido de marca no puede ser privado: regla de
TikTok, aplicada aquí.

---

## Sonido

### ¿Puede ponerle un sonido viral a mis vídeos automáticamente?

**No.** Ninguna API pública de TikTok deja adjuntar un sonido del catálogo
general, que es de donde salen los virales; el único campo que existe
(`musicSoundInfo` de Zernio) apunta a la *Commercial Music Library*, un catálogo
licenciado distinto que no da ningún empujón de alcance. La única forma de poner
un sonido viral es hacerlo a mano en el editor de la app de TikTok.

La v2 tenía un «Sound Boost» que te sugería un sonido y enviaba el vídeo a
borradores para que lo pusieras tú. Se quitó en la v3: no hacía lo que su nombre
prometía y obligaba a terminar a mano cada publicación.

### ¿No puede meter el audio en el archivo con ffmpeg antes de subirlo?

Podría, pero **no serviría de nada**: el audio incrustado es una pista más del
vídeo, no el sonido de TikTok, así que no apareces en la página de ese sonido,
que es el único mecanismo real que había detrás de la idea.

---

## Analíticas y comentarios

### ¿Qué números veo de cada vídeo?

Reproducciones, me gusta, comentarios, compartidos y % de interacción. Si la
cuenta está conectada por el carril de Business, además vistas de perfil y % de
vídeo visto entero, que TikTok da con 24–48 h de retraso.

### ¿Por qué no hay impresiones, alcance ni tiempo de visualización?

Porque no están en ninguna API pública de TikTok, ni siquiera para cuentas
Business. Son exclusivos de TikTok Studio. La Research API tampoco los expone y
está limitada a uso académico.

### ¿Me fío de «tus mejores horas»?

Con pocos vídeos, no. Es la media de reproducciones por hora de publicación, y
un solo vídeo viral arrastra su hora entera. Por eso sólo se tienen en cuenta
las horas con al menos 3 vídeos detrás. Aun así, es una correlación: con 20
vídeos publicados no hay nada que concluir.

### ¿Los comentarios llegan solos?

Se consultan cada 30 minutos, de los vídeos publicados en los últimos 14 días.
No hay webhooks porque harían falta una URL pública y TLS, y esto corre en
`127.0.0.1`. Entre la caché de Zernio (10 min) y el intervalo, un comentario
nuevo puede tardar unos 40 minutos en aparecer.

### ¿Se generan solos el texto y los hashtags?

Sí, desde la 3.1, para todo vídeo que entre en la cola **sin texto**. Se
transcribe en local con Whisper y, si tienes Ollama, un modelo local escribe el
gancho y propone los hashtags del tema; si no, el texto es la primera frase del
vídeo. Ocurre al entrar en la cola, lo ves progresar en la propia cola y puedes
corregirlo antes de que salga. Todo en [CAPTIONS.md](CAPTIONS.md).

### ¿Y si prefiero escribir yo el texto de algunos vídeos?

Escríbelo en un `.txt` junto al vídeo al subirlo a la biblioteca y se usa tal
cual: no se genera nada. Para un vídeo concreto,
**Texto** sobrescribe lo generado (título, descripción, hashtags y comentario
fijado, con la revisión y la vista previa del editor), y si lo haces mientras se
está generando, lo tuyo gana.

### ¿De dónde salen los hashtags? ¿De verdad «aportan audiencia»?

Fijos de Ajustes → del tema (Ollama o las palabras del vídeo, más el banco
`data/hashtags.json`) → un par de alcance. No hay ninguna fuente pública de
audiencias de hashtags en TikTok (el Creative Center pide sesión desde 2026), así
que la única medida real es la de tu cuenta: los hashtags con 3 o más vídeos
publicados se puntúan por su media de reproducciones y los flojos se caen. Lo
ves en Analíticas → Qué hashtags funcionan. Aprende con el tiempo; el primer
mes es una hipótesis razonable, no una certeza.

### El texto tarda en aparecer / el vídeo dice «esperando al texto»

Whisper `small` tarda unos segundos por minuto de voz; la primera vez además
descarga el modelo (460 MB, hay un aviso en Actividad). Ollama con un modelo de
7B tarda otros pocos segundos; con uno de 26B, decenas. Un vídeo espera a su
texto como mucho `CAPTION_MAX_WAIT_MINUTES` (10) y luego sale con lo que tenga.

### ¿Necesito Ollama?

No. Sin Ollama el texto es la primera frase del vídeo, que en un reel hablado
suele ser justo el gancho, y los hashtags salen de las palabras más repetidas y
del banco de temas. Con Ollama (`brew install ollama && ollama pull
qwen2.5:7b-instruct`) el texto es un gancho escrito de verdad. Se detecta solo.

---

## Administración

### ¿Para qué es el navegador de base de datos?

Para ver y corregir lo que hay dentro sin abrir una terminal: qué vídeos hay en
cada cola, por qué falló uno, qué reglas tiene una cuenta. Edición fila a fila y
consola SQL de sólo lectura → [ADMIN.md](ADMIN.md#base-de-datos).

### ¿Cómo hago una copia de seguridad?

No hace falta: con la app abierta se hace **una al día** sola (se guardan las 14
últimas). **Ajustes → Copias → Crear copia ahora** hace otra cuando quieras, y
deja un archivo en `data/backups/` que puedes descargar. Para restaurar, para la
app y sustituye `data/scheduler.db` (borrando antes `scheduler.db-wal` y
`-shm`). Para guardarlo todo fuera del Mac —o llevártelo a otro—, `make
snapshot` añade la clave de cifrado y el `.env` ([INSTALL.md](INSTALL.md#llevarte-tu-instalación-a-otro-mac)).

### ¿Qué pasa al actualizar la herramienta?

Al arrancar, la base de datos se migra sola: columnas nuevas, y las migraciones
estructurales (la consolidación en Zernio de la versión 2, las varias colas por
cuenta de la 2.1, y la retirada de borradores, privacidad y Sound Boost de la
3.0). Lo verás en la terminal como `database migrated`. Ninguna migración borra
vídeos ni actividad, pero haz una copia antes si te da tranquilidad.

### Configuré el webhook en Ajustes pero no llegaba nada

Era un fallo de versiones antiguas. Ahora manda lo que pongas en Ajustes y, si
está vacío, el `.env`. **Enviar aviso de prueba** lo comprueba.

---

## Seguridad

### ¿Dónde está mi contraseña de TikTok?

En ningún sitio. La introduces en el dominio de TikTok cuando conectas la cuenta
en Zernio.

### ¿Se puede exponer a internet?

**No lo hagas.** Escucha en `127.0.0.1` a propósito. Si necesitas acceder desde
fuera, usa un túnel SSH o una VPN. Más detalle en [SECURITY.md](SECURITY.md).

### ¿Puedo revocar el acceso?

Sí: la API key en Zernio, la conexión con TikTok en *TikTok → Ajustes →
Seguridad → Aplicaciones conectadas*, y la cuenta aquí con **Cuentas → Eliminar**.

---

## Límites y riesgos

### ¿Me pueden banear por usar esto?

Zernio usa la API oficial de TikTok con tu propia autorización, que es justo lo
contrario de un bot. El riesgo no está en la herramienta sino en el **ritmo**:
publicar cada 5 minutos es sospechoso venga de donde venga. Los presets están en
rangos sensatos por eso.

### ¿Funciona en Windows / Linux?

El código es Python multiplataforma. Los scripts `run.sh` y `demo.sh` son bash:
en Windows usa WSL, o lanza directamente `python -m app.main`.

---

## Desarrollo

### ¿Cómo lo pruebo sin cuenta de Zernio?

```bash
./tools/demo.sh
```

Levanta un Zernio falso en local y la app apuntando a él en
<http://127.0.0.1:8790>. Pega la clave `zernio-test-key`: lista dos cuentas
falsas y puedes recorrer el flujo entero sin que salga nada a internet.

### ¿Cómo ejecuto los tests?

```bash
./.venv/bin/python -m pytest -q
```

### ¿Puedo usarlo desde otro programa?

Sí, hay API JSON. Con la app en marcha, la documentación interactiva está en
<http://127.0.0.1:8787/docs>, y las rutas más útiles en [ADMIN.md](ADMIN.md#desde-otro-programa).
