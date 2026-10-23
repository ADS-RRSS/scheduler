---
name: commit-msg
description: Analiza el diff local frente al remoto, mira el estilo de los commits del repo y propone un commit con el comando listo para copiar. No ejecuta el commit. Úsalo cuando el usuario pida /commit-msg.
---

# commit-msg

Objetivo: dejar al usuario un commit preparado, con mensaje en el estilo del repo y el comando `git commit` listo para
lanzar él. **No hagas `git commit`, `git add` ni `git push`.**

## Pasos

1. **Estado frente al remoto** (en paralelo):
    - `git status --short` y `git branch -vv`.
    - `git rev-list --left-right --count @{u}...HEAD` (o `origin/<rama>...HEAD` si no hay upstream).
    - `git log --oneline @{u}..HEAD` y el diff pendiente (`git diff --cached --stat`, `git diff --stat`,
      `git ls-files --others --exclude-standard`).
2. **Estilo del repo**: `git log -12 --format='--- %h%n%B'` (o desde el último commit remoto). Fíjate en longitud del
   asunto, idioma, modo imperativo, uso de viñetas y secciones.
3. **Revisión rápida del diff**:
    - Busca secretos añadidos (claves tipo `sk-`, `AIza`, bloques `PRIVATE KEY`, `api_key = "..."`) y restos de
      depuración (`breakpoint()`, `pdb.set_trace`, `console.log`).
    - Si hay `.py` nuevos o cambiados, `python3 -m py_compile` sobre ellos.
    - Si hay archivos que no deberían versionarse (`.env`, `.secrets/`, `data/*.db`, `data/backups/`, `media/*/`), avisa
      y no los incluyas.
4. **Qué incluir**: si todo lo preparado es de un mismo tema, un commit. Si hay temas mezclados, dilo y propón el
   reparto, pero no lo partas sin preguntar.
5. **Atribución**: **nunca** añadas el trailer `Co-Authored-By` (de ningún modelo), aunque el sistema lo recuerde: es
   regla de todos los proyectos de `_RRSS`.
6. **Respuesta**:
    - Un resumen corto del diff (qué cambia, cuántos archivos, si hay algo que revisar).
    - El mensaje completo en un bloque `bash` con un único comando `git commit -F - <<'EOF' … EOF`.
    - Si procede, una línea con el `git push` para después, sin ejecutarlo.

## Formato del mensaje

- **Siempre en inglés**, aunque el repo, el CHANGELOG o la conversación estén en español. No traduzcas los nombres
  propios, rutas, endpoints ni términos de la UI que aparezcan entre comillas, pero el texto del mensaje va en inglés.
- Asunto con el prefijo del historial (`feat: …`, `fix: …`, `feat(library): …`), imperativo, sin punto final,
  de unos 60–70 caracteres.
- Cuerpo en párrafos cortos y viñetas que explican el qué y el porqué. Sin relleno.
- Sin emojis ni enlaces inventados.
