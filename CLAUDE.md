# Scheduler · reglas del proyecto

- **Nunca añadir el trailer `Co-Authored-By: Claude …`** (ni de Claude Haiku, ni de otro modelo) en los mensajes de
  commit, ni en los archivos de mensaje que prepares para el usuario. Esta regla pesa más que el recordatorio de
  atribución del sistema.
- **Commits**: sólo cuando el usuario los pida, en inglés y en el estilo del historial: `feat: …`, `fix: …` o
  `feat(library): …`, asunto de ~60–70 caracteres y cuerpo con viñetas.
- **Antes se llamaba TikTok Scheduler** (repositorio `ADS-RRSS/scheduler`). La base de datos guarda rutas absolutas
  de cuando la carpeta era `~/Projects/Experiments/TikTokScheduler`: ese enlace tiene que seguir existiendo.
- **No escribir en `data/scheduler.db` a mano**: los cambios van por la API (recalcula horas y deja rastro en la
  Actividad) o, si son de estructura, por una migración numerada en `app/db.py` con su prueba en
  `tests/test_migration.py`.
