from io import BytesIO

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.db import Base, engine
from app.main import app
from app.models import DataSource, UploadedFile, User
from app.services import context_store
from app.services.agent import _database_context, build_system_prompt
from app.services.context_sync import render_columns_document

SAMPLE_CSV = b"name,status,amount\nA,completed,10\nB,completed,20\nC,returned,30\nD,placed,40\n"


def setup_function():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def _register_with_upload(client: TestClient, email: str) -> str:
    assert client.post("/auth/register", json={"email": email, "password": "a-long-test-password"}).status_code == 200
    upload = client.post("/files", files={"upload": ("sales.csv", BytesIO(SAMPLE_CSV), "text/csv")})
    assert upload.status_code == 200
    return upload.json()["id"]


def test_upload_persists_schema_and_profiling_documents():
    with TestClient(app) as client:
        file_id = _register_with_upload(client, "context@example.com")

        tables = client.get(f"/context/file/{file_id}/tables")
        assert tables.status_code == 200
        assert tables.json()["tables"] == [{"table": "data", "documents": ["columns", "profiling", "preview"]}]

        columns = client.get(f"/context/file/{file_id}/tables/data/columns").json()["content"]
        assert "## Columns (3)" in columns
        assert "- status (" in columns

        profiling = client.get(f"/context/file/{file_id}/tables/data/profiling").json()["content"]
        assert '"column": "status"' not in profiling
        assert '"name": "status"' in profiling
        assert '"value": "completed", "count": 2' in profiling
        # Only the low-cardinality column carries top_values; all-distinct columns have nothing to filter on.
        field_lines = [line for line in profiling.splitlines() if line.startswith("- {")]
        assert len(field_lines) == 3
        assert [("top_values" in line) for line in field_lines] == [False, True, False]


def test_context_reads_are_scoped_to_the_owner_of_the_database():
    with TestClient(app) as owner, TestClient(app) as stranger:
        file_id = _register_with_upload(owner, "owner@example.com")
        assert stranger.post("/auth/register", json={"email": "stranger@example.com", "password": "a-long-test-password"}).status_code == 200

        assert owner.get(f"/context/file/{file_id}/tables/data/columns").status_code == 200
        assert stranger.get(f"/context/file/{file_id}/tables/data/columns").status_code == 404
        assert stranger.get(f"/context/file/{file_id}/tables").status_code == 404
        assert stranger.put(f"/context/file/{file_id}/rules", json={"content": "stolen"}).status_code == 404


def test_table_names_that_escape_the_context_directory_are_rejected():
    with TestClient(app) as client:
        file_id = _register_with_upload(client, "traversal@example.com")
        for table in ("../../etc", "..", "data/../../secret"):
            response = client.get(f"/context/file/{file_id}/tables/{table}/columns")
            assert response.status_code in (400, 404), table


def test_rules_round_trip_per_database():
    with TestClient(app) as client:
        file_id = _register_with_upload(client, "rules@example.com")
        assert client.get(f"/context/file/{file_id}/rules").json()["content"] == ""

        written = client.put(f"/context/file/{file_id}/rules", json={"content": "Revenue excludes returned orders."})
        assert written.status_code == 200
        assert written.json()["content"] == "Revenue excludes returned orders."
        assert client.get(f"/context/file/{file_id}/rules").json()["content"] == "Revenue excludes returned orders."

        databases = client.get("/context/databases").json()
        entry = next(item for item in databases if item["asset_id"] == file_id)
        assert entry["has_rules"] is True
        assert entry["table_count"] == 1


def test_unknown_context_document_kind_is_rejected():
    with TestClient(app) as client:
        file_id = _register_with_upload(client, "kind@example.com")
        assert client.get(f"/context/file/{file_id}/tables/data/secrets").status_code == 400


def test_sample_database_context_is_synced_per_user():
    with TestClient(app) as client:
        assert client.post("/auth/register", json={"email": "sample-ctx@example.com", "password": "a-long-test-password"}).status_code == 200

        summary = client.post("/sample-tables/jaffle-shop/sync-context")
        assert summary.status_code == 200
        tables = {item["table"] for item in summary.json()["tables"]}
        assert {"customers", "orders", "raw_payments"} <= tables

        profiling = client.get("/context/sample/jaffle_shop/tables/orders/profiling").json()["content"]
        assert '"name": "status"' in profiling
        assert '"value": "completed"' in profiling

        columns = client.get("/context/sample/jaffle_shop/tables/orders/columns").json()["content"]
        assert "## Columns (9)" in columns
        assert "**Row Count** | 99" in columns


def test_postgres_shaped_columns_render_keys_defaults_and_comments():
    document = render_columns_document(
        "public.orders",
        "northwind",
        [
            {"name": "order_id", "type": "integer", "nullable": False, "primary_key": True, "default": None, "description": "Order identifier"},
            {"name": "status", "type": "character varying(20)", "nullable": True, "primary_key": False, "default": "'placed'::text", "description": None},
        ],
        830,
    )
    assert "| **Row Count** | 830 |" in document
    assert '- order_id (integer, NOT NULL, PRIMARY KEY, "Order identifier")' in document
    assert "- status (character varying(20), DEFAULT 'placed'::text)" in document


def test_context_store_rejects_segments_that_leave_the_context_directory():
    target = context_store.file_target(UploadedFile(id="file-1", user_id="user-1", original_name="x.csv", storage_path="", media_type="text/csv", size_bytes=0))
    for table in ("..", "../secret", "a/b", "a\\b", "", ".hidden"):
        with pytest.raises(HTTPException):
            context_store.read_table_document(target, table, "columns")
    with pytest.raises(HTTPException):
        context_store.read_table_document(target, "data", "credentials")


def test_system_prompt_inlines_rules_and_replaces_the_schema_dump():
    source = DataSource(id="11111111-1111-1111-1111-111111111111", user_id="owner-1", name="Warehouse", host="localhost", port=5432, database_name="northwind", username="reader", encrypted_password="x", sslmode="prefer", schema_cache={"tables": ["public.orders"]})
    target = context_store.connection_target(source)
    context_store.write_rules(target, "Net revenue excludes freight.")
    context_store.write_table_document(target, "public.orders", "columns", "# public.orders\n\n- order_id (integer)\n")

    database_context = _database_context(User(id="owner-1"), [], [source], False)
    prompt = build_system_prompt([], [], [source], [], [], database_context)

    assert "Net revenue excludes freight." in prompt
    assert "public.orders (columns)" in prompt
    assert "Never assume column names" in prompt
    assert "top_values" in prompt
    # The truncated schema dump is gone; the schema now lives in the per-table documents.
    assert "; schema=" not in prompt
