"""A small phpMyAdmin-like browser for the SQLite database.

Everything goes through SQLAlchemy's reflection so it works on whatever tables
exist, and every identifier is validated against that reflection before it is
interpolated into SQL. Values are read raw (as SQLite stores them) so JSON and
datetime columns show up as the text they really are.

Secrets (``*_enc`` columns) are masked on the way out and refused on the way in.
"""
from __future__ import annotations

import csv
import io
import json
import re
import sqlite3
import time

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import engine, get_session
from ..schemas import DbRowUpdate, SqlIn
from ..services import events

router = APIRouter(prefix="/api/db", tags=["db"])

SECRET_SUFFIX = "_enc"
MASK = "••••••••"
MAX_PER_PAGE = 200
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_READ_ONLY_PREFIXES = ("select", "with", "explain", "pragma")


def _is_secret(column: str) -> bool:
    return column.endswith(SECRET_SUFFIX)


def _q(identifier: str) -> str:
    if not _IDENT_RE.match(identifier):
        raise HTTPException(400, f"Identificador inválido: {identifier}")
    return f'"{identifier}"'


def _table_or_404(name: str) -> dict:
    inspector = inspect(engine)
    if name not in inspector.get_table_names():
        raise HTTPException(404, f"La tabla «{name}» no existe.")
    pk = inspector.get_pk_constraint(name).get("constrained_columns") or []
    columns = []
    for col in inspector.get_columns(name):
        kind = str(col["type"]).upper()
        columns.append({
            "name": col["name"],
            "type": kind,
            "nullable": bool(col.get("nullable", True)),
            "pk": col["name"] in pk,
            "secret": _is_secret(col["name"]),
            "default": str(col["default"]) if col.get("default") is not None else None,
        })
    return {"name": name, "columns": columns, "pk": pk[0] if len(pk) == 1 else None}


def _clean(value, column: str):
    if _is_secret(column) and value is not None:
        return MASK
    if isinstance(value, bytes):
        return f"<blob {len(value)} bytes>"
    return value


def _row_dict(columns: list[str], row) -> dict:
    return {col: _clean(val, col) for col, val in zip(columns, row)}


def _coerce(column: dict, value):
    """Turn a JSON value from the editor into something SQLite will store."""
    if value is None:
        return None
    kind = column["type"]
    if "INT" in kind or "BOOL" in kind:
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, (int, float)):
            return int(value)
        text_value = str(value).strip().lower()
        if text_value in ("true", "1", "yes", "sí", "si"):
            return 1
        if text_value in ("false", "0", "no"):
            return 0
        try:
            return int(text_value)
        except ValueError as exc:
            raise HTTPException(400, f"«{column['name']}» debe ser un entero.") from exc
    if any(k in kind for k in ("REAL", "FLOA", "DOUB", "NUMERIC", "DECIMAL")):
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, f"«{column['name']}» debe ser numérico.") from exc
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


# --------------------------------------------------------------------------- #
# Tables
# --------------------------------------------------------------------------- #

@router.get("/tables")
def list_tables(session: Session = Depends(get_session)) -> dict:
    inspector = inspect(engine)
    tables = []
    for name in sorted(inspector.get_table_names()):
        count = session.execute(text(f"SELECT COUNT(*) FROM {_q(name)}")).scalar() or 0
        tables.append({"name": name, "rows": count,
                       "columns": len(inspector.get_columns(name))})
    return {"tables": tables}


@router.get("/tables/{table}")
def read_table(
    table: str, page: int = 1, per_page: int = 50, sort: str | None = None,
    dir: str = "asc", q: str | None = None, session: Session = Depends(get_session),
) -> dict:
    meta = _table_or_404(table)
    names = [c["name"] for c in meta["columns"]]
    per_page = max(1, min(per_page, MAX_PER_PAGE))
    page = max(1, page)

    where, params = "", {}
    if q:
        searchable = [c for c in names if not _is_secret(c)]
        where = " WHERE " + " OR ".join(f"CAST({_q(c)} AS TEXT) LIKE :q" for c in searchable)
        params["q"] = f"%{q}%"

    order = ""
    if sort:
        if sort not in names:
            raise HTTPException(400, f"No existe la columna «{sort}».")
        order = f" ORDER BY {_q(sort)} {'DESC' if dir.lower() == 'desc' else 'ASC'}"
    elif meta["pk"]:
        order = f" ORDER BY {_q(meta['pk'])} DESC"

    total = session.execute(text(f"SELECT COUNT(*) FROM {_q(table)}{where}"), params).scalar() or 0
    rows = session.execute(
        text(f"SELECT * FROM {_q(table)}{where}{order} LIMIT :limit OFFSET :offset"),
        {**params, "limit": per_page, "offset": (page - 1) * per_page},
    ).fetchall()
    return {
        "table": table,
        "columns": meta["columns"],
        "pk": meta["pk"],
        "rows": [_row_dict(names, row) for row in rows],
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": max(1, -(-total // per_page)),
    }


@router.get("/tables/{table}/export.csv")
def export_csv(table: str, session: Session = Depends(get_session)):
    meta = _table_or_404(table)
    names = [c["name"] for c in meta["columns"]]
    rows = session.execute(text(f"SELECT * FROM {_q(table)}")).fetchall()

    def generate():
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(names)
        yield buffer.getvalue()
        for row in rows:
            buffer.seek(0)
            buffer.truncate()
            writer.writerow([_clean(v, c) for c, v in zip(names, row)])
            yield buffer.getvalue()

    return StreamingResponse(
        generate(), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{table}.csv"'},
    )


def _pk_or_400(meta: dict) -> str:
    if not meta["pk"]:
        raise HTTPException(400, "Esta tabla no tiene una clave primaria simple.")
    return meta["pk"]


@router.get("/tables/{table}/rows/{pk}")
def read_row(table: str, pk: str, session: Session = Depends(get_session)) -> dict:
    meta = _table_or_404(table)
    key = _pk_or_400(meta)
    names = [c["name"] for c in meta["columns"]]
    row = session.execute(
        text(f"SELECT * FROM {_q(table)} WHERE {_q(key)} = :pk"), {"pk": pk}
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Fila no encontrada.")
    return {"table": table, "pk": key, "row": _row_dict(names, row), "columns": meta["columns"]}


@router.put("/tables/{table}/rows/{pk}")
def update_row(
    table: str, pk: str, body: DbRowUpdate, session: Session = Depends(get_session)
) -> dict:
    meta = _table_or_404(table)
    key = _pk_or_400(meta)
    by_name = {c["name"]: c for c in meta["columns"]}
    assignments, params = [], {"pk": pk}
    for column, value in body.values.items():
        if column not in by_name:
            raise HTTPException(400, f"No existe la columna «{column}».")
        if column == key:
            raise HTTPException(400, "La clave primaria no se puede editar.")
        if _is_secret(column):
            raise HTTPException(400, f"«{column}» es un secreto cifrado: edítalo desde Cuentas.")
        params[f"v_{column}"] = _coerce(by_name[column], value)
        assignments.append(f"{_q(column)} = :v_{column}")
    if not assignments:
        raise HTTPException(400, "No hay nada que actualizar.")
    result = session.execute(
        text(f"UPDATE {_q(table)} SET {', '.join(assignments)} WHERE {_q(key)} = :pk"), params
    )
    if result.rowcount == 0:
        session.rollback()
        raise HTTPException(404, "Fila no encontrada.")
    events.record(session, "db_row_updated",
                  f"Fila {pk} de «{table}» editada desde el navegador de base de datos "
                  f"({', '.join(body.values)}).", level="warn")
    session.commit()
    return read_row(table, pk, session)


@router.delete("/tables/{table}/rows/{pk}")
def delete_row(table: str, pk: str, session: Session = Depends(get_session)) -> dict:
    meta = _table_or_404(table)
    key = _pk_or_400(meta)
    result = session.execute(
        text(f"DELETE FROM {_q(table)} WHERE {_q(key)} = :pk"), {"pk": pk}
    )
    if result.rowcount == 0:
        session.rollback()
        raise HTTPException(404, "Fila no encontrada.")
    events.record(session, "db_row_deleted",
                  f"Fila {pk} de «{table}» borrada desde el navegador de base de datos.",
                  level="warn")
    session.commit()
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Read-only SQL console
# --------------------------------------------------------------------------- #

def _strip_comments(sql: str) -> str:
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    sql = re.sub(r"--[^\n]*", " ", sql)
    return sql.strip()


def validate_read_only(sql: str) -> str:
    """Accept exactly one SELECT / WITH / EXPLAIN / read-only PRAGMA statement."""
    clean = _strip_comments(sql).rstrip(";").strip()
    if not clean:
        raise HTTPException(400, "Consulta vacía.")
    if ";" in clean:
        raise HTTPException(400, "Sólo se admite una sentencia por consulta.")
    head = clean.split(None, 1)[0].lower()
    if head not in _READ_ONLY_PREFIXES:
        raise HTTPException(400, "Sólo se admiten consultas de lectura (SELECT, WITH, EXPLAIN, PRAGMA). "
                                 "Para editar usa la tabla.")
    if head == "pragma" and "=" in clean:
        raise HTTPException(400, "Sólo PRAGMA de lectura (sin «=»).")
    return clean


@router.post("/query")
def run_query(body: SqlIn) -> dict:
    settings = get_settings()
    path = settings.database_path
    if path is None:
        raise HTTPException(400, "La consola SQL sólo funciona con SQLite.")
    sql = validate_read_only(body.sql)

    # A dedicated read-only connection: even a statement that slipped through
    # the prefix check cannot write.
    con = sqlite3.connect(str(path), timeout=5)
    try:
        con.execute("PRAGMA query_only = 1")
        started = time.perf_counter()
        try:
            cursor = con.execute(sql)
            rows = cursor.fetchmany(body.limit + 1)
        except sqlite3.Error as exc:
            raise HTTPException(400, f"SQLite: {exc}") from exc
        elapsed = (time.perf_counter() - started) * 1000
        columns = [d[0] for d in (cursor.description or [])]
    finally:
        con.close()

    truncated = len(rows) > body.limit
    rows = rows[: body.limit]
    return {
        "columns": columns,
        "rows": [[_clean(v, c) for c, v in zip(columns, row)] for row in rows],
        "count": len(rows),
        "truncated": truncated,
        "elapsed_ms": round(elapsed, 2),
    }
