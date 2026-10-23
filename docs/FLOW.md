# Cómo funciona por dentro

Diagramas del recorrido completo, de un archivo en tu disco a un vídeo en TikTok.

---

## 1 · Vista general

```mermaid
flowchart LR
    subgraph TU["Tu ordenador"]
        A["Biblioteca<br/>media/library/&lt;serie&gt;/"]
        B["Colas de cada cuenta<br/>SQLite"]
        C["Planificador<br/>ciclo cada 20 s"]
        D["Reglas por cola<br/>intervalo · máx/día<br/>franjas · días<br/>+ topes de la cuenta"]
    end
    subgraph Z["Zernio"]
        E["API<br/>zernio.com/api/v1"]
    end
    F["TikTok<br/>tu perfil, en público"]

    A -->|"«Añadir desde la biblioteca»<br/>textos ya escritos"| B
    B --> C
    D --> C
    C -->|"cuando toca el hueco"| E
    E --> F
    E -.->|"estado + URL"| C
    E -.->|"métricas cada 3 h<br/>comentarios cada 30 min"| B
    C -.->|"avisos"| G["Discord · Slack<br/>ntfy · Telegram"]
```

---

## 2 · Conectar una cuenta

```mermaid
sequenceDiagram
    participant U as Tú
    participant Z as zernio.com
    participant A as Scheduler

    U->>Z: Conectas tu TikTok (OAuth en tiktok.com)<br/>y creas una API key
    U->>A: Pegas la API key
    A->>Z: GET /v1/accounts?platform=tiktok
    Z-->>A: cuentas de TikTok conectadas
    U->>A: Marcas las que quieres
    Note over A: Una fila por cuenta, con su<br/>primera cola. La key se cifra con<br/>Fernet; nunca sale por /api/state.
    A-->>U: Cuentas conectadas ✓
```

Cada cuenta se conecta **una sola vez**. Para publicarla con varios horarios se le
añaden colas (`Programación → + Nueva cola`): misma cuenta, misma clave, misma
conexión con Zernio, pero listas de vídeos y reglas independientes.

```mermaid
flowchart LR
    ACC["Cuenta de TikTok<br/>clave · máx/día · separación mínima"]
    Q1["Cola «Día»<br/>10:00 – 22:00<br/>sus vídeos"]
    Q2["Cola «Noche»<br/>22:00 – 10:00<br/>sus vídeos"]
    ACC --> Q1
    ACC --> Q2
```

---

## 3 · El ciclo del planificador

Cada 20 segundos (configurable), en este orden:

```mermaid
flowchart TD
    START([Nuevo ciclo]) --> LIB["Registrar en la biblioteca lo copiado<br/>a mano en media/library (≥5 s sin cambiar)"]
    LIB --> POLL["Consultar en Zernio los posts<br/>aceptados que aún no tienen URL"]
    POLL --> EXTRA["¿Toca? → analíticas (3 h)<br/>y comentarios (30 min)"]
    EXTRA --> PAUSED{"¿Pausado?"}
    PAUSED -->|sí| END([Esperar al siguiente])
    PAUSED -->|no| LOOP["Para cada cuenta activa,<br/>en orden de creación"]

    LOOP --> FLIGHT{"¿Hay una subida<br/>en curso en la cuenta?"}
    FLIGHT -->|sí| END
    FLIGHT -->|no| FORCED{"¿Alguien pulsó «Ahora»<br/>en alguna cola?"}
    FORCED -->|sí| PUB
    FORCED -->|no| WALK["Recorrer las colas<br/>de la cuenta en orden"]
    WALK --> ENABLED{"¿Cola activa<br/>y con vídeos?"}
    ENABLED -->|no| NEXTQ{"¿Queda<br/>otra cola?"}
    NEXTQ -->|sí| WALK
    NEXTQ -->|no| END
    ENABLED -->|sí| DUE{"¿Ha llegado el hueco<br/>del primer vídeo?"}
    DUE -->|no| NEXTQ
    DUE -->|sí| PUB["Entregar a Zernio"]
    PUB --> REPLAN["Recalcular el hueco de<br/>TODAS las colas de la cuenta"]
    REPLAN --> END
```

Sólo se publica **un vídeo por ciclo y cuenta**, venga de la cola que venga: así
una subida grande no bloquea la consulta de estado del resto, y dos colas de la
misma cuenta nunca suben a la vez. Si varias colas tienen el hueco vencido sale
la del hueco **más antiguo**, no la primera de la lista. Las cuentas
**desactivadas** se saltan enteras.

Tras publicar se recalculan **todas** las colas de esa cuenta: lo que una reserva
consume presupuesto diario de la cuenta y ocupa una hora de la que las demás
tienen que mantenerse a distancia, así que las demás se mueven.

Las consultas de estado se hacen **incluso en pausa**: esas subidas ya salieron y
hay que saber cómo acabaron. Lo mismo vale para las analíticas y los comentarios:
son de sólo lectura, tienen su propio reloj y no publican nada.

---

## 4 · Cálculo del siguiente hueco

Es la parte con más lógica y la única que no puedes comprobar a ojo, por eso es
la que más tests tiene (38, incluidos los cambios de hora).

```mermaid
flowchart TD
    A([Calcular hueco]) --> B{"¿Hay publicación<br/>anterior en esta cola?"}
    B -->|no| C["candidato = ahora"]
    B -->|sí| D["candidato = anterior +<br/>aleatorio(mín, máx)"]
    D --> E["Respetar el hueco<br/>mínimo absoluto"]
    C --> F
    E --> F["candidato = máx(candidato, ahora)"]

    F --> G{"¿Ese día de la<br/>semana está activo?"}
    G -->|no| H["Saltar a las 00:00<br/>del día siguiente"]
    H --> G

    G -->|sí| I{"¿Alcanzado el máximo<br/>diario de la cola<br/>o el de la cuenta?"}
    I -->|sí| H
    I -->|no| S{"¿Está a menos de la<br/>separación mínima de algo<br/>que la CUENTA ya tiene?"}
    S -->|sí| S2["Saltar justo detrás<br/>de esa reserva"]
    S2 --> G
    S -->|no| J{"¿Hay franjas<br/>horarias?"}

    J -->|no| K["Sumar jitter aleatorio<br/>0 … N segundos"]
    J -->|sí| L{"¿El candidato cae<br/>dentro de una franja?"}
    L -->|sí| O{"¿Se llegó saltando a la<br/>apertura de la franja<br/>o del día?"}
    O -->|no| K
    O -->|sí| P["Retrasar 0 … 30 min<br/>(como mucho ¼ de lo<br/>que queda de franja)"]
    P --> G
    L -->|no| M["Saltar al inicio de<br/>la siguiente franja"]
    M --> G

    K --> N([Hueco definitivo])
```

Detalles que importan:

- **El día se cuenta en tu zona horaria**, no en UTC. "6 al día" son 6 días de
  Madrid, que es lo que uno espera.
- **Las franjas que cruzan medianoche** (22:00→06:00) se tratan como un único
  tramo continuo. El contador diario se reinicia a medianoche, en mitad de la
  franja, porque el máximo es "por día natural".
- **Lo que espera a que abra una franja no sale en punto.** Si el hueco cae
  fuera de franja (o el día está lleno), el salto a la apertura añade de 0 a
  30 minutos al azar, nunca más de un cuarto de lo que queda de franja, y se
  vuelve a comprobar todo (separación, máximo diario). Así unas franjas
  7–10 y 18–22 no publican cada día a las 07:00 y a las 18:00 clavadas.
  `OPENING_SPREAD_MINUTES` en `scheduler/planner.py`.
- **El jitter siempre suma, nunca resta.** Restar podría colocar el hueco en el
  pasado o incumplir el intervalo mínimo que acabamos de aplicar.
- **Cambios de hora**: el cálculo se hace sobre instantes reales, así que nunca
  devuelve una hora imposible ni se queda en bucle.
- **La cadencia se reinicia desde cada publicación real**, también las forzadas
  con «Ahora». Un «Ahora» que falla no consume el hueco programado.
- **Las colas de una cuenta corren a la vez, no una detrás de otra.** La
  separación mínima es la distancia a las publicaciones vecinas de la cuenta, no
  un turno: una cola nueva coloca su primer vídeo hoy, entre los huecos que dejó
  la otra, aunque la otra tenga dos semanas reservadas o entregadas a Zernio.
- **Sólo hay una separación mínima y está en la cuenta.** Vale para dos vídeos
  seguidos de la misma cola y para dos vídeos de colas distintas, así que no hay
  forma de que una cola publique más apretado de lo que la cuenta permite.
- **El máximo diario de la cuenta sí es un presupuesto compartido.** Entra como
  un contador extra que suma lo de todas las colas: dos colas de 5 al día en una
  cuenta con tope 6 dan 6 publicaciones, no 10. Las últimas colas del día se
  quedan sin hueco y la interfaz lo avisa al guardar.

---

## 5 · Publicar un vídeo

```mermaid
sequenceDiagram
    participant S as Planificador
    participant P as Publicador
    participant Z as zernio.com/api/v1
    participant R2 as Almacén temporal<br/>de Zernio

    S->>P: publicar(vídeo)
    P->>P: Validar con ffprobe
    alt El archivo no es válido
        P-->>S: FALLIDO · se mueve a media/failed/
    end
    P->>P: Generar el texto<br/>(variantes + campos + hashtags)
    P->>Z: POST /media/presign<br/>{filename, contentType, size}
    Z-->>P: uploadUrl + publicUrl
    P->>R2: PUT uploadUrl (streaming,<br/>Content-Length fijo)
    P->>Z: POST /posts {content, mediaItems,<br/>platforms[tiktok, accountId],<br/>tiktokSettings{privacy_level: PUBLIC,<br/>allow_*, consent flags, disclosure},<br/>publishNow}
    Z-->>P: 201 · status: published<br/>(URL aún vacía)
    Note over P: Estado → PROCESANDO
    loop Cada 30 s, hasta 5 min
        S->>Z: GET /posts/{id}
        Z-->>S: platformPostUrl cuando TikTok la expone
    end
    Note over S: Estado → PUBLICADO<br/>archivo a media/posted/ · aviso
```

---

## 5 bis · El texto, mientras el vídeo espera

```mermaid
stateDiagram-v2
    [*] --> none: entra con texto tuyo<br/>(o IA desactivada)
    [*] --> pending: entra sin texto
    pending --> generating: le toca (uno a uno)
    generating --> done: Whisper (+ Ollama) → texto + hashtags
    generating --> failed: sin archivo, sin modelo, error
    pending --> failed: pasó CAPTION_MAX_WAIT_MINUTES<br/>y el vídeo salió sin él
    done --> pending: «Regenerar»
    failed --> pending: «Regenerar»
    pending --> none: escribes el texto a mano
    generating --> none: escribes el texto a mano<br/>(lo generado se descarta)
    none --> pending: «Escribir solo»
```

El publicador mira `caption_status` antes de subir: `pending` o `generating` es
«todavía no», hasta el tope de minutos. Lo demás sale.

---

## 6 · Estados de un vídeo

```mermaid
stateDiagram-v2
    [*] --> EN_COLA: se añade a la cola
    [*] --> FALLIDO: no pasa la validación

    EN_COLA --> PROGRAMADO: se le asigna un hueco
    PROGRAMADO --> EN_COLA: cambias la configuración<br/>o lo mueves de cuenta

    PROGRAMADO --> SUBIENDO: llega su hora
    SUBIENDO --> PROCESANDO: Zernio lo acepta
    SUBIENDO --> PROGRAMADO: fallo temporal → reintento
    SUBIENDO --> FALLIDO: fallo definitivo
    SUBIENDO --> PUBLICADO: Zernio confirma con URL al instante
    PROCESANDO --> PUBLICADO: llega la URL<br/>o pasan 5 min
    PROCESANDO --> FALLIDO: TikTok lo rechaza<br/>o no hay respuesta en 60 min

    EN_COLA --> EN_ZERNIO: mandas la cola entera<br/>(con su hora calculada)
    PROGRAMADO --> EN_ZERNIO: ídem
    EN_ZERNIO --> EN_ZERNIO: se sube a Zernio<br/>(uno por ciclo)
    EN_ZERNIO --> EN_COLA: «Recuperar»<br/>o fallo que lo devuelve
    EN_ZERNIO --> PUBLICADO: Zernio lo publica a su hora
    EN_ZERNIO --> FALLIDO: archivo inválido, o 48 h<br/>sin confirmación

    FALLIDO --> EN_COLA: pulsas «Reintentar»
    PUBLICADO --> [*]
```

**EN_ZERNIO** (`DELEGATED`) es el único estado que no depende de esta app: el
vídeo tiene hora y Zernio lo publicará aunque el ordenador esté apagado. Mientras
no tenga `zernio_post_id` es que sus bytes aún no han salido de aquí; a partir de
ahí, lo único que hace la app es preguntar por él cuando su hora ha pasado.

### Qué se reintenta y qué no

| Tipo de error  | Ejemplos                                                                          | Qué hace                                                               |
|----------------|-----------------------------------------------------------------------------------|------------------------------------------------------------------------|
| **Temporal**   | `429` de Zernio, `5xx`, caídas de red                                             | Reintenta con espera creciente (5 → 10 → 20 min, o lo que pida Zernio) |
| **Definitivo** | archivo inválido, `400`, `402` (plan), `409` (duplicado), TikTok rechaza el vídeo | No reintenta. El archivo va a `media/failed/`                          |
| **De cuenta**  | `401` (clave revocada)                                                            | No reintenta. **Cuentas → Sustituir clave**                            |

En todos los casos **el hueco programado se consume**. Si no fuese así, un vídeo
defectuoso haría que el siguiente saliese inmediatamente después, rompiendo tu
cadencia justo cuando menos te conviene.

---

## 7 · Después de publicar

Un vídeo publicado no se olvida: se le siguen pidiendo los números y los
comentarios mientras sea reciente.

```mermaid
flowchart TD
    PUB["Vídeo PUBLICADO<br/>con su URL de TikTok"] --> AN & CM

    subgraph AN["Analíticas · cada 3 h"]
        A1["GET /analytics?platform=tiktok<br/>&accountId&fromDate"] --> A2["Emparejar por<br/>id de post de Zernio"]
        A2 --> A3[("video_metrics<br/>1 fila por vídeo,<br/>sobrescrita")]
        A3 --> A4["Totales · ranking<br/>media por hora y día"]
    end

    subgraph CM["Comentarios · cada 30 min"]
        C0{"¿Publicado hace<br/>menos de 14 días?"} -->|no| C9([Se deja en paz])
        C0 -->|sí| C1["GET /inbox/comments/{videoId}<br/>máx. 5 vídeos por cuenta"]
        C1 --> C2[("comments<br/>únicos por vídeo + id externo")]
        C2 --> C3["Pendientes = ajenos,<br/>no ocultos, sin respuesta mía"]
        C3 --> C4["Responder / Ocultar<br/>→ POST a Zernio"]
    end
```

El id que pide el buzón es el **id de vídeo de TikTok**, que se saca de la URL
(`/video/<id>`); si aún no hay URL, ese vídeo no se consulta. Zernio cachea la
lista de comentarios 10 minutos, así que uno nuevo puede tardar hasta unos 40
minutos en aparecer aquí.

---

## 8 · Dónde vive cada cosa

```mermaid
flowchart TB
    subgraph UI["Interfaz · navegador"]
        HTML["index.html<br/>app.js · admin.js · shared.js"]
    end
    subgraph API_["API · FastAPI"]
        R1["routes_api.py<br/>estado, cuentas, cola, reglas"]
        R2["routes_admin.py<br/>sistema, copias, mantenimiento"]
        R3["routes_db.py<br/>navegador de base de datos"]
    end
    subgraph SVC["Servicios"]
        S1["publisher.py<br/>ciclo de publicación"]
        S2["queue.py<br/>cola y archivos"]
        S3["media.py<br/>validación ffprobe"]
        S4["captions.py<br/>plantillas de texto"]
        S4b["autocaption.py<br/>Whisper → Ollama"]
        S4c["hashtags.py<br/>elección de hashtags"]
        S5["analytics.py<br/>métricas y agregados"]
        S5b["comments.py<br/>buzón de comentarios"]
        S6["scheduling.py<br/>modelo ↔ planificador"]
        S7["zernio.py<br/>cliente de Zernio"]
        S8["accounts.py<br/>cuentas y claves"]
        S9["notify.py<br/>avisos"]
    end
    subgraph CORE["Núcleo"]
        P["planner.py<br/>matemática pura<br/>sin BD ni red"]
        W["worker.py<br/>bucle de fondo"]
        CW["captioner.py<br/>bucle de textos"]
    end
    subgraph EXT["Fuera"]
        ZN["zernio.com"]
        OL["Ollama<br/>127.0.0.1:11434"]
        DB[("SQLite<br/>+ migraciones")]
    end

    HTML -->|"GET /api/state<br/>cada 5 s"| R1
    HTML --> R2
    HTML --> R3
    R1 --> SVC
    R3 --> DB
    W --> S1
    S1 --> S7
    S8 --> S7
    S7 --> ZN
    S6 --> P
    SVC --> DB
    W --> DB
    CW --> S4b
    S4b --> S4c
    S4b -.-> OL
    CW --> DB
```

`planner.py` no importa nada de la base de datos ni de la red a propósito: así se
puede probar de forma exhaustiva sin montar nada. Ahí está toda la lógica de
"cuándo toca publicar".
