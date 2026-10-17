"""Generate the persisted context documents for one database.

Sampling is per source type; rendering and profiling are shared, so a column
document reads the same whether it came from PostgreSQL, a spreadsheet or the
bundled sample database.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pandas as pd
from fastapi import HTTPException
from sqlalchemy import text

from ..config import settings
from ..models import DataSource, UploadedFile
from . import context_store
from .analytics import profile_frame
from .context_store import ContextTarget
from .jaffle_shop import database_path as jaffle_shop_database_path
from .query import engine_for_source

_IDENTIFIER_QUOTE = '"'


def sync_connection_context(source: DataSource, profile: bool = True) -> dict:
    """Write columns/preview/profiling documents for every readable table."""
    target = context_store.connection_target(source)
    engine = engine_for_source(source)
    skipped: list[dict] = []
    try:
        with engine.connect() as connection:
            connection.execute(text("SET TRANSACTION READ ONLY"))
            tables = _postgres_tables(connection)
            context_store.clear_tables(target)
            for schema_name, table_name in tables:
                try:
                    _sync_postgres_table(connection, target, source.database_name, schema_name, table_name, profile)
                except Exception as exc:
                    # One unreadable table must not abandon the rest of the database.
                    connection.rollback()
                    connection.execute(text("SET TRANSACTION READ ONLY"))
                    skipped.append({"table": f"{schema_name}.{table_name}", "reason": str(exc)[:200]})
    finally:
        engine.dispose()
    return _summary(target) | {"skipped": skipped}


def _sync_postgres_table(connection, target: ContextTarget, database: str, schema_name: str, table_name: str, profile: bool) -> None:
    qualified = f"{schema_name}.{table_name}"
    columns = _postgres_columns(connection, schema_name, table_name)
    row_count = _postgres_row_count(connection, schema_name, table_name)
    sample = _postgres_sample(connection, schema_name, table_name)
    context_store.write_table_document(
        target, qualified, "columns", render_columns_document(qualified, database, columns, row_count)
    )
    context_store.write_table_document(
        target, qualified, "preview", render_preview_document(qualified, database, sample)
    )
    if profile:
        context_store.write_table_document(
            target, qualified, "profiling", render_profiling_document(qualified, database, profile_frame(sample), len(sample))
        )


def sync_file_context(file: UploadedFile) -> dict:
    target = context_store.file_target(file)
    frame = _read_file_frame(Path(file.storage_path))
    table = "data"
    columns = [{"name": str(name), "type": str(frame[name].dtype), "nullable": True} for name in frame.columns]
    context_store.clear_tables(target)
    context_store.write_table_document(
        target, table, "columns", render_columns_document(table, file.original_name, columns, len(frame))
    )
    context_store.write_table_document(
        target, table, "preview", render_preview_document(table, file.original_name, frame.head(settings.context_preview_rows))
    )
    context_store.write_table_document(
        target, table, "profiling", render_profiling_document(table, file.original_name, profile_frame(frame), len(frame))
    )
    return _summary(target)


def sync_sample_context(user_id: str) -> dict:
    target = context_store.sample_target(user_id)
    connection = duckdb.connect(str(jaffle_shop_database_path()), read_only=True)
    try:
        names = [row[0] for row in connection.execute("SHOW TABLES").fetchall()]
        context_store.clear_tables(target)
        for name in names:
            columns = [
                {"name": row[0], "type": row[1], "nullable": row[2] != "NO"}
                for row in connection.execute(f"DESCRIBE {_quote(name)}").fetchall()
            ]
            row_count = connection.execute(f"SELECT count(*) FROM {_quote(name)}").fetchone()[0]
            sample = connection.execute(
                f"SELECT * FROM {_quote(name)} LIMIT {settings.context_profile_sample_rows}"
            ).df()
            context_store.write_table_document(
                target, name, "columns", render_columns_document(name, "jaffle_shop", columns, row_count)
            )
            context_store.write_table_document(
                target, name, "preview", render_preview_document(name, "jaffle_shop", sample.head(settings.context_preview_rows))
            )
            context_store.write_table_document(
                target, name, "profiling", render_profiling_document(name, "jaffle_shop", profile_frame(sample), len(sample))
            )
    finally:
        connection.close()
    return _summary(target)


def render_columns_document(table: str, database: str, columns: list[dict], row_count: int | None) -> str:
    lines = [f"# {table}", "", f"**Database:** `{database}`", ""]
    if row_count is not None:
        lines += ["## Table Metadata", "", "| Property | Value |", "|----------|-------|", f"| **Row Count** | {row_count:,} |", ""]
    lines += [f"## Columns ({len(columns)})", ""]
    for column in columns:
        lines.append(f"- {column['name']} ({_column_annotations(column)})")
    lines.append("")
    return "\n".join(lines)


def render_profiling_document(table: str, database: str, profile: dict, sampled_rows: int) -> str:
    lines = [
        f"# {table} - Profiling",
        "",
        f"**Database:** `{database}`",
        "",
        f"**Computed at:** `{datetime.now(UTC).isoformat()}`",
        "",
        f"**Sampled rows:** {sampled_rows:,}",
        "",
        f"**Columns:** {profile.get('column_count', 0)}",
        "",
        f"**Duplicate rows in sample:** {_count(profile.get('duplicate_rows'))}",
        "",
        "## Column Profiles (JSONL)",
        "",
        "> `top_values` lists the values actually present. Filter on these exact values; never guess an enum or status string.",
        "",
    ]
    for field in profile.get("fields", []):
        lines.append(f"- {json.dumps(field, ensure_ascii=False, default=str)}")
    lines.append("")
    return "\n".join(lines)


def render_preview_document(table: str, database: str, rows: pd.DataFrame) -> str:
    limited = rows.head(settings.context_preview_rows)
    lines = [f"# {table} - Preview", "", f"**Database:** `{database}`", "", f"## Rows ({len(limited)})", ""]
    for record in limited.to_dict("records"):
        lines.append(f"- {json.dumps(record, ensure_ascii=False, default=str)}")
    lines.append("")
    return "\n".join(lines)


def _count(value: int | None) -> str:
    """Statistics are None for columns pandas cannot hash, such as json holding dicts."""
    return f"{value:,}" if value is not None else "not computable for these column types"


def _column_annotations(column: dict) -> str:
    parts = [str(column.get("type", "unknown"))]
    if column.get("nullable") is False:
        parts.append("NOT NULL")
    if column.get("primary_key"):
        parts.append("PRIMARY KEY")
    if column.get("default"):
        parts.append(f"DEFAULT {column['default']}")
    if column.get("description"):
        parts.append(f'"{column["description"]}"')
    return ", ".join(parts)


def _postgres_tables(connection) -> list[tuple[str, str]]:
    rows = connection.execute(
        text(
            "SELECT table_schema, table_name FROM information_schema.tables "
            "WHERE table_schema NOT IN ('pg_catalog', 'information_schema') "
            "ORDER BY table_schema, table_name LIMIT :limit"
        ),
        {"limit": settings.context_max_tables},
    ).all()
    return [(row[0], row[1]) for row in rows]


def _postgres_columns(connection, schema_name: str, table_name: str) -> list[dict]:
    rows = connection.execute(
        text(
            "SELECT c.column_name, c.data_type, c.character_maximum_length, c.numeric_precision, "
            "       c.numeric_scale, c.is_nullable, c.column_default, "
            "       (kcu.column_name IS NOT NULL) AS is_primary_key, "
            "       col_description(format('%I.%I', c.table_schema, c.table_name)::regclass, c.ordinal_position) AS description "
            "FROM information_schema.columns c "
            "LEFT JOIN information_schema.table_constraints tc "
            "  ON tc.table_schema = c.table_schema AND tc.table_name = c.table_name "
            " AND tc.constraint_type = 'PRIMARY KEY' "
            "LEFT JOIN information_schema.key_column_usage kcu "
            "  ON kcu.constraint_name = tc.constraint_name AND kcu.column_name = c.column_name "
            "WHERE c.table_schema = :schema AND c.table_name = :table "
            "ORDER BY c.ordinal_position"
        ),
        {"schema": schema_name, "table": table_name},
    ).mappings().all()
    return [
        {
            "name": row["column_name"],
            "type": _postgres_type(row),
            "nullable": row["is_nullable"] == "YES",
            "primary_key": bool(row["is_primary_key"]),
            "default": row["column_default"],
            "description": row["description"],
        }
        for row in rows
    ]


def _postgres_type(row) -> str:
    data_type = row["data_type"]
    if row["character_maximum_length"]:
        return f"{data_type}({row['character_maximum_length']})"
    if data_type == "numeric" and row["numeric_precision"]:
        return f"numeric({row['numeric_precision']},{row['numeric_scale'] or 0})"
    return data_type


def _postgres_row_count(connection, schema_name: str, table_name: str) -> int | None:
    try:
        return connection.execute(
            text(f"SELECT count(*) FROM {_quote(schema_name)}.{_quote(table_name)}")
        ).scalar_one()
    except Exception:
        return None


def _postgres_sample(connection, schema_name: str, table_name: str) -> pd.DataFrame:
    result = connection.execute(
        text(
            f"SELECT * FROM {_quote(schema_name)}.{_quote(table_name)} LIMIT {int(settings.context_profile_sample_rows)}"
        )
    )
    columns = list(result.keys())
    return pd.DataFrame(result.mappings().all(), columns=columns)


def _read_file_frame(path: Path) -> pd.DataFrame:
    if not path.is_file():
        raise HTTPException(404, "The stored file is no longer available")
    if path.suffix.lower() == ".csv":
        return pd.read_csv(path, sep=None, engine="python")
    return pd.read_excel(path, sheet_name=0)


def _quote(identifier: str) -> str:
    escaped = identifier.replace(_IDENTIFIER_QUOTE, _IDENTIFIER_QUOTE * 2)
    return f"{_IDENTIFIER_QUOTE}{escaped}{_IDENTIFIER_QUOTE}"


def _summary(target: ContextTarget) -> dict:
    tables = context_store.list_tables(target)
    return {"target": target.reference, "label": target.label, "table_count": len(tables), "tables": tables}
