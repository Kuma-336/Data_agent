"""Read-only access to the jaffle_shop sample database shipped with this repo."""

from __future__ import annotations

from pathlib import Path

import duckdb
from fastapi import HTTPException
from sqlglot import exp, parse_one

from ..config import settings


def database_path() -> Path:
    """Resolve the explicitly configured database, or the repository sample."""
    if settings.jaffle_shop_database_path:
        path = settings.jaffle_shop_database_path
    else:
        # config.py lives at <repo>/data_agent/backend/app/config.py.
        path = Path(__file__).resolve().parents[4] / "example" / "jaffle_shop.duckdb"
    if not path.is_file():
        raise HTTPException(503, "The jaffle_shop sample database is not available")
    return path


def list_tables() -> list[dict]:
    path = database_path()
    connection = duckdb.connect(str(path), read_only=True)
    try:
        names = [row[0] for row in connection.execute("SHOW TABLES").fetchall()]
        tables = []
        for name in names:
            columns = [
                {"name": row[0], "type": row[1]}
                for row in connection.execute(f'DESCRIBE "{name}"').fetchall()
            ]
            count = connection.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
            tables.append({"name": name, "row_count": count, "columns": columns})
        return tables
    finally:
        connection.close()


def execute_query(sql: str, allowed_tables: list[str], max_rows: int) -> tuple[list[str], list[dict], str]:
    if not allowed_tables:
        raise HTTPException(400, "Select at least one jaffle_shop table before querying it")
    try:
        tree = parse_one(sql, read="duckdb")
    except Exception as exc:
        raise HTTPException(400, f"Invalid SQL: {exc}") from exc
    ctes = {cte.alias_or_name for cte in tree.find_all(exp.CTE)}
    referenced = {table.name for table in tree.find_all(exp.Table) if table.name not in ctes}
    unknown = referenced - set(allowed_tables)
    if unknown:
        raise HTTPException(403, f"Query references a table that was not selected: {', '.join(sorted(unknown))}")

    connection = duckdb.connect(str(database_path()), read_only=True)
    try:
        result = connection.execute(sql)
        columns = [item[0] for item in result.description]
        rows = [dict(zip(columns, row, strict=True)) for row in result.fetchmany(max_rows)]
        return columns, rows, sql
    except Exception as exc:
        raise HTTPException(400, f"jaffle_shop query failed: {exc}") from exc
    finally:
        connection.close()
