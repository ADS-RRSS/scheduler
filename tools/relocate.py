#!/usr/bin/env python3
"""Pasa las rutas de los vídeos de la carpeta de antes del Scheduler a esta (tras copiarlo a otro Mac).

    ./.venv/bin/python tools/relocate.py --from /Users/antes/…/Scheduler            # sólo dice lo que haría
    ./.venv/bin/python tools/relocate.py --from /Users/antes/…/Scheduler --apply    # lo escribe

La base de datos guarda rutas absolutas: copiada a otra carpeta (otro Mac, otro
usuario), apuntarían a la de antes. Cada ruta bajo la carpeta de antes pasa a
la misma bajo esta, historial incluido; nada más cambia, y la Actividad lo
apunta. Con la app cerrada (la usa el instalador de Tunnel al migrar). Lanzarlo
dos veces no hace nada la segunda.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import BASE_DIR  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.services import relocation  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--from", dest="old", required=True, help="la carpeta del Scheduler de antes")
    parser.add_argument("--to", dest="new", default=str(BASE_DIR), help="la de ahora (por defecto, ésta)")
    parser.add_argument("--apply", action="store_true", help="escribirlo (sin él, sólo lo cuenta)")
    args = parser.parse_args()
    old, new = Path(args.old), Path(args.new)
    if old.resolve() == new.resolve():
        print("La carpeta es la misma: no hay nada que mover.")
        return 0
    init_db()
    with SessionLocal() as session:
        if not args.apply:
            n = len(relocation.plan(session, old, new))
            print(f"{n} rutas pasarían de {old} a {new}. Con --apply se escriben.")
            return 0
        n = relocation.relocate(session, old, new)
        session.commit()
    print(f"✓ {n} rutas pasan de {old} a {new}." if n else "Ninguna ruta apuntaba a la carpeta de antes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
