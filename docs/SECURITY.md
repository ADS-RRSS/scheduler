# Seguridad

Qué se guarda, dónde, y qué protege cada cosa. Sin exagerar las garantías.

---

## Tu contraseña de TikTok

**Nunca pasa por esta aplicación** ni por Zernio. La autorización ocurre en
`tiktok.com` cuando conectas la cuenta en el panel de Zernio. Aquí sólo llega
una API key de Zernio.

---

## La API key de Zernio

|         |                                                                                     |
|---------|-------------------------------------------------------------------------------------|
| Qué es  | Una credencial que permite publicar en las cuentas de TikTok conectadas a tu Zernio |
| Cifrado | Fernet (AES-128-CBC + HMAC-SHA256)                                                  |
| Clave   | `.secrets/encryption.key`, creada con permisos `600`                                |
| Dónde   | `data/scheduler.db`, columna `accounts.api_key_enc`, siempre cifrada                |
| Caduca  | No; la revocas tú en Zernio                                                         |

La API local **no la devuelve** en `/api/state` ni en el navegador de base de
datos (aparece enmascarada). El único sitio donde se muestra es
**Cuentas → Mostrar clave**, y cada vez que se usa queda un aviso en Actividad.

La clave de cifrado se crea con `os.open(..., O_EXCL, 0o600)`, de modo que no
existe ni un instante con permisos abiertos. Si prefieres gestionarla tú, ponla
en el `.env`:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

```ini
APP_ENCRYPTION_KEY=la_clave_generada
```

**Qué protege esto**: que alguien que copie tu `scheduler.db` (una copia de
seguridad, un disco perdido) no pueda usar tu API key.

**Qué NO protege**: a alguien con acceso completo a tu usuario del sistema, que
podría leer la clave y la base de datos igual que tú, o abrir la interfaz y
pulsar **Mostrar clave**. Para eso está el código de acceso.

---

## El servidor

Escucha en **`127.0.0.1`**, no en `0.0.0.0`: sólo es accesible desde tu propio
ordenador.

### Código de acceso (recomendado)

```ini
UI_PASSCODE=algo_que_recuerdes
```

Activa una pantalla de login con cookie firmada (HMAC con la clave de cifrado) y
comparación en tiempo constante. **Ponlo si alguien más usa el ordenador**: las
páginas de administración muestran la API key, editan la base de datos y borran
filas.

Sé realista sobre qué aporta: es una cerradura en una puerta que ya está dentro de
tu casa. **No convierte la app en algo seguro de exponer a internet.**

### No lo expongas a internet

No hay límite de peticiones ni protección anti-fuerza bruta. Cualquiera que
llegue al puerto con el código podría publicar en tus cuentas. Si necesitas
acceder desde fuera, usa un túnel SSH:

```bash
ssh -L 8787:127.0.0.1:8787 usuario@tu-servidor
```

Y entra en `http://127.0.0.1:8787` desde tu máquina. Nunca abras el puerto en el
router ni pongas `HOST=0.0.0.0`.

### Las otras webs que tengas abiertas

Que el servidor sólo escuche en `127.0.0.1` no impide que **una página de
internet abierta en tu navegador** le hable: cualquier web puede hacer que el
navegador mande un `POST` a `http://127.0.0.1:8787` (un formulario, o un `fetch`
sin cuerpo, que no necesita permiso). No vería la respuesta, pero la petición
llegaría: «publicar ahora», «entregar la cola a Zernio», «pausar». Y con un
nombre que primero apunta a su servidor y luego a `127.0.0.1` (*DNS rebinding*)
podría incluso leer las respuestas.

Por eso, delante de todo lo demás (`app/security.py`, `request_guard`):

- Una petición que **cambia algo** (POST, PUT, PATCH, DELETE) sólo se atiende si
  viene de las páginas de la propia app: el navegador lo dice en
  `Sec-Fetch-Site` y en `Origin`. Si no, `403 Petición bloqueada: viene de otra
  página web`. `curl` y las herramientas no mandan esas cabeceras y pasan: no
  son un navegador llevando la página de otro.
- Toda petición tiene que ir dirigida a **un nombre de la app** (`Host`):
  `127.0.0.1` y `localhost` siempre, más los de `ALLOWED_HOSTS` (separados por
  comas; `*` = cualquiera). Si no, `400 Host no permitido`. Los de loopback no
  corren peligro: una página que hace *rebinding* manda su propio nombre.

### En Docker y en un VPS

- Dentro del contenedor la app escucha en `0.0.0.0`, pero `docker-compose.yml`
  **sólo publica el puerto en `127.0.0.1`** del equipo, y fija
  `ALLOWED_HOSTS=127.0.0.1,localhost`.
- En un VPS: `UI_PASSCODE` obligatorio, el puerto en `127.0.0.1` y acceso por
  túnel SSH o por un proxy con HTTPS. Detrás de un proxy, pon el dominio en
  `DOCKER_ALLOWED_HOSTS` y deja que el proxy conserve `Host` (o mande
  `X-Forwarded-Host`). Ver [INSTALL.md](INSTALL.md#en-un-vps-o-un-equipo-siempre-encendido).
- Si la app arranca escuchando fuera del equipo sin código o sin
  `ALLOWED_HOSTS`, lo dice en la terminal al arrancar.

---

## El navegador de base de datos

- Sólo refleja y consulta las tablas que existen; cada identificador se valida
  contra esa reflexión antes de usarse en SQL, y los valores van como parámetros.
- Las columnas `*_enc` se enmascaran al leer y se rechazan al escribir.
- La consola SQL admite una sola sentencia de lectura y corre en una conexión
  aparte con `PRAGMA query_only=1`: aunque una sentencia se saltara el filtro, el
  motor rechazaría escribir.
- Cada edición y borrado de filas queda registrado en Actividad.

Aun así, editar la base de datos a mano es editar la base de datos a mano. Haz
una copia antes (**Ajustes → Crear copia ahora**).

---

## Lo que esta herramienta no hace

Por diseño:

- **No automatiza el navegador.** Nada de Selenium ni Playwright contra tiktok.com.
- **No usa cookies de sesión** ni APIs internas no documentadas de TikTok.
- **No envía datos a ningún sitio que no hayas configurado tú.** Las conexiones
  salientes son exactamente dos: `zernio.com` (y su almacén temporal de archivos)
  y la URL de avisos (Discord, Slack, ntfy, Telegram) si la configuras.
- **No hay telemetría.**
- **No acepta nada entrante.** No hay webhooks de vuelta: las analíticas y los
  comentarios se consultan desde aquí, así que no hace falta abrir ningún puerto
  ni exponer la herramienta a internet.
- **El vídeo no sale de tu ordenador para escribir su texto.** Whisper corre en
  local y Ollama también (`127.0.0.1`). La única conexión extra es la descarga
  de los pesos de Whisper desde Hugging Face, una vez, a `data/models/whisper/`.
  Si apuntas `OLLAMA_URL` a otra máquina, la transcripción viaja hasta ella.

### Qué implica usar Zernio

Zernio recibe **el vídeo, el texto y la configuración de cada post**, y publica
en tu nombre con la autorización que tú le diste en TikTok. Con las analíticas y
el buzón activados, la herramienta además le pide **los números de tus vídeos y
los comentarios que recibes**, y le manda **las respuestas que escribes**. Nada
de eso sale de la pareja herramienta ↔ Zernio, pero sí pasa por Zernio.

Es un tercero con acceso real a tu cuenta; su política de privacidad, no este
documento, dice qué hace con eso. Si dejas de usarlo: revoca la API key en su
panel, quita Zernio en *TikTok → Ajustes → Seguridad → Aplicaciones conectadas*,
y **Cuentas → Eliminar** aquí para borrar la clave cifrada.

Si prefieres no darle acceso al buzón, `COMMENTS_ENABLED=false` en `.env` apaga
la lectura de comentarios, y `ANALYTICS_REFRESH_MINUTES=0` la de analíticas. La
publicación sigue funcionando igual.

---

## Copias de seguridad

La app copia la base de datos **sola, una vez al día** mientras está abierta
(`data/backups/scheduler-auto-FECHA.db`; se guardan las 14 últimas, con
`BACKUP_AUTO_HOURS` y `BACKUP_KEEP`), y **Ajustes → Crear copia ahora** hace una
a mano (`scheduler-FECHA.db`), que nunca se borra sola. Todas usan la copia en
caliente de SQLite. La base de datos es lo único que dice qué post de Zernio es
cada vídeo: sin ella no se pueden recuperar los entregados.

Para conservarlo todo fuera del ordenador —o llevártelo a otro— está
**`make snapshot`**: un solo archivo con la base de datos (copia consistente),
`.secrets/encryption.key`, `.env` y el banco de hashtags, escrito con permisos
`600` y con la suma SHA-256 de cada pieza. `make restore FILE=…` lo pone en su
sitio (ver [INSTALL.md](INSTALL.md#llevarte-tu-instalación-a-otro-mac)).

Ese archivo contiene tu API key cifrada y la clave para descifrarla. Trátalo como
una contraseña. Si restauras una copia **sin** `encryption.key`, las claves serán
ilegibles y tendrás que sustituirlas en Cuentas; la cola y la configuración se
conservan.

---

## Reportar un problema

Es un proyecto personal sin canal formal. Si encuentras un fallo de seguridad,
lo sensato es dejar de usar la herramienta, revocar la API key en Zernio y
revisar el código antes de volver a conectarla.
