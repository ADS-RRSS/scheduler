#!/usr/bin/env python3
"""Verifica la copia de la biblioteca, borra los originales de media/ y actualiza la BD.

    ./.venv/bin/python tools/library_finalize.py            # sólo comprueba
    ./.venv/bin/python tools/library_finalize.py --apply    # borra lo verificado

Por cada vídeo que siga en media/inbox, media/posted o media/failed:

1. busca su copia en media/library/<serie>/ y la compara byte a byte (y
   comprueba que su .txt también está);
2. si es idéntica, cambia a la ruta de la biblioteca todas las filas de
   video_items que apuntaban al original, lo guarda, y sólo entonces borra el
   original (y su .txt, si la copia es idéntica).

Lo que no se puede verificar se queda donde está y se lista al final. Antes
de tocar nada se guarda una copia de la base de datos en data/backups.

Con la aplicación abierta se niega a borrar (podría estar subiendo uno de
esos archivos): ciérrala antes, o usa --force si sabes que no hay subidas.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.db import SessionLocal, init_additive  # noqa: E402
from app.services import library_migration as migration  # noqa: E402


def _progress(stage: str, done: int, total: int) -> None:
    if done == total or done % 50 == 0:
        print(f"  {stage}… {done}/{total}", file=sys.stderr, flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true",
                        help="borrar los originales verificados (sin esto sólo se comprueba)")
    parser.add_argument("--force", action="store_true",
                        help="borrar aunque la aplicación esté abierta")
    args = parser.parse_args()

    settings = get_settings()
    if args.apply and migration.app_is_running(settings) and not args.force:
        print(f"La aplicación está abierta en {settings.base_url}. Ciérrala antes de borrar "
              "(o usa --force si no hay ninguna subida en marcha).")
        return 2
    if args.apply:
        backup = migration.backup_database(settings)
        if backup is not None:
            print(f"Copia de la base de datos: {backup}")
        init_additive()
        session = SessionLocal()
        try:
            report = migration.finalize(session, settings, apply=True, progress=_progress)
        finally:
            session.close()
    else:
        # On a throwaway copy of the database: checking changes nothing at all.
        with migration.scratch_session(settings) as session:
            report = migration.finalize(session, settings, apply=False, progress=_progress)

    gb = 1024 ** 3
    print()
    print(f"Verificados (copia idéntica en la biblioteca): {report.verified}")
    if args.apply:
        print(f"Originales borrados: {report.deleted} ({report.bytes_freed / gb:.1f} GB)")
        print(f".txt borrados: {report.texts_deleted}")
        print(f"Filas de video_items que apuntan ya a la biblioteca: {report.rows_repointed}")
    else:
        print(f"Se borrarían: {report.deleted} vídeo(s) ({report.bytes_freed / gb:.1f} GB) "
              f"y {report.texts_deleted} .txt sueltos")
        print(f"Filas de video_items que cambiarían de ruta: {report.rows_repointed}")
    if report.kept:
        print(f"\nSe quedan en media/ ({len(report.kept)}):")
        for line in report.kept:
            print("  · " + line)
    for note in report.notes:
        print("  ⚠ " + note)
    if not args.apply:
        print("\nNada se ha borrado. Repite con --apply para hacerlo.")
    return 1 if report.kept else 0


if __name__ == "__main__":
    raise SystemExit(main())
