import json
import re
from pathlib import Path
import duckdb
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session
from fastapi import HTTPException
from ..config import settings
from ..models import DataSource, QueryResult, UploadedFile, User
from ..security import decrypt_secret
from .sql_guard import validate_readonly_sql


def _json_safe(rows: list[dict]) -> list[dict]:
    return json.loads(json.dumps(rows, default=str))


def execute_local(db: Session, user: User, chat_id: str, file: UploadedFile, sql: str) -> QueryResult:
    safe_sql = validate_readonly_sql(sql, settings.max_query_rows)
    if re.search(r"\browid\b", safe_sql, re.IGNORECASE):
        raise HTTPException(400, "Local CSV/XLSX tables have no implicit rowid column. Use only the columns returned by inspect_file, quoted with double quotes when needed.")
    path = Path(file.storage_path)
    conn = duckdb.connect()
    try:
        if path.suffix.lower() == ".csv":
            # Follow nao's local-query approach: expose the verified storage file to
            # DuckDB before running user SQL, instead of interpolating or binding a
            # path inside CREATE VIEW (DuckDB cannot prepare DDL parameters).
            conn.read_csv(str(path)).create_view("data")
        else:
            # Avoid a DuckDB extension dependency in the runtime image. The first sheet is
            # registered as `data`; inspect_file exposes every available sheet to the agent.
            import pandas as pd
            conn.register("data", pd.read_excel(path, sheet_name=0))
        result = conn.execute(safe_sql)
        columns = [item[0] for item in result.description]
        rows = [dict(zip(columns, row, strict=True)) for row in result.fetchall()]
    except Exception as exc:
        raise HTTPException(400, f"Local query failed: {exc}") from exc
    finally:
        conn.close()
    return _save_result(db, user.id, chat_id, columns, rows, safe_sql, f"file:{file.id}")


def execute_postgres(db: Session, user: User, chat_id: str, source: DataSource, sql: str) -> QueryResult:
    if source.host.lower() not in settings.allowed_hosts:
        raise HTTPException(403, "Database host is not allowlisted")
    safe_sql = validate_readonly_sql(sql, settings.max_query_rows)
    url = URL.create(
        "postgresql+psycopg",
        username=source.username,
        password=decrypt_secret(source.encrypted_password),
        host=source.host,
        port=source.port,
        database=source.database_name,
        query={"sslmode": source.sslmode},
    )
    engine = create_engine(url, connect_args={"options": "-c statement_timeout=30000"})
    try:
        with engine.connect() as connection:
            connection.execute(text("SET TRANSACTION READ ONLY"))
            result = connection.execute(text(safe_sql))
            columns = list(result.keys())
            rows = [dict(row) for row in result.mappings().all()]
    except Exception as exc:
        raise HTTPException(400, f"PostgreSQL query failed: {exc}") from exc
    finally:
        engine.dispose()
    return _save_result(db, user.id, chat_id, columns, rows, safe_sql, f"postgres:{source.id}")


def _save_result(db: Session, user_id: str, chat_id: str, columns: list[str], rows: list[dict], sql: str, source: str) -> QueryResult:
    record = QueryResult(user_id=user_id, chat_id=chat_id, columns=columns, rows=_json_safe(rows), sql=sql, source=source)
    db.add(record)
    db.commit()
    db.refresh(record)
    return record
