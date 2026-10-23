#!/usr/bin/env python3
"""Copia a la biblioteca todos los vídeos (y sus .txt) de media/inbox, posted y failed.

    ./.venv/bin/python tools/library_copy.py            # sólo dice lo que haría
    ./.venv/bin/python tools/library_copy.py --apply    # copia y registra

Cada vídeo va a media/library/<serie>/, una sola vez aunque estuviera en dos
colas (el «…-1.mp4» de YouTube es el mismo archivo), con su .txt. Cada uno
recibe su fila en la biblioteca con los textos que ya tenían sus colas, y esas
filas quedan enlazadas a él. Los originales NO se tocan: para borrarlos, una
vez verificada la copia, está tools/library_finalize.py.

En APFS (el disco de un Mac) la copia es un clon: comparte los bloques con el
original y no ocupa espacio hasta que uno de los dos se borra. «du» contará
los dos; «df» dice la verdad.

Puede ejecutarse con la aplicación abierta (sólo añade una tabla y una
columna nuevas, y lee los originales) y todas las veces que haga falta: lo
que ya está copiado no se copia otra vez.
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
                        help="copiar de verdad (sin esto sólo se informa)")
    parser.add_argument("--list", action="store_true",
                        help="enumerar cada archivo que se copiaría")
    args = parser.parse_args()

    settings = get_settings()
    print(f"Biblioteca: {settings.media_library_dir}")
    print("Origen:     " + ", ".join(str(d) for d in settings.legacy_media_dirs))
    if args.apply:
        backup = migration.backup_database(settings)
        if backup is not None:
            print(f"Copia de la base de datos: {backup}")
        init_additive()
        session = SessionLocal()
        try:
            report = migration.copy_all(session, settings, dry_run=False, progress=_progress)
            session.commit()
        finally:
            session.close()
    else:
        # On a throwaway copy of the database: a dry run changes nothing at all.
        with migration.scratch_session(settings) as session:
            report = migration.copy_all(session, settings, dry_run=True, progress=_progress)

    gb = 1024 ** 3
    verb = "Copiados" if args.apply else "Se copiarían"
    print()
    print(f"Vídeos distintos en media/: {report.groups}")
    print(f"{verb}: {report.copied} ({report.bytes_copied / gb:.1f} GB)"
          + (f", {report.cloned} como clon APFS (sin ocupar espacio)" if report.cloned else ""))
    print(f"Ya estaban en la biblioteca: {report.already}")
    print(f"Duplicados que se quedan en uno: {report.bytes_saved / gb:.1f} GB menos")
    if args.apply:
        print(f"Filas nuevas en la biblioteca: {report.registered}")
        print(f"Vídeos de las colas enlazados: {report.linked_rows}")
        print(f".txt copiados: {report.texts_copied}")
    if report.to_write:
        print(f"Sin textos todavía (nadie los tenía en cola): {report.to_write}; se escribirán "
              "solos al abrir la aplicación si los textos automáticos están activos")
    if report.folders:
        print(f"Carpetas: {len(report.folders)}")
    if args.list:
        for line in report.planned:
            print("  " + line)
    for problem in report.problems:
        print("  ⚠ " + problem)
    if not args.apply:
        print("\nNada se ha copiado. Repite con --apply para hacerlo.")
    return 1 if report.problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
