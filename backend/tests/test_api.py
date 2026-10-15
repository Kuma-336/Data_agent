from io import BytesIO
from fastapi.testclient import TestClient
from app.db import Base, engine
from app.main import app
from app.models import AgentRun, DataSource
from app.db import SessionLocal


def setup_function():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def test_register_upload_and_user_isolation():
    with TestClient(app) as first, TestClient(app) as second:
        register = first.post("/auth/register", json={"email": "first@example.com", "password": "a-long-test-password"})
        assert register.status_code == 200
        upload = first.post("/files", files={"upload": ("sales.csv", BytesIO(b"name,amount\nA,10\n"), "text/csv")})
        assert upload.status_code == 200
        file_id = upload.json()["id"]
        assert second.post("/auth/register", json={"email": "second@example.com", "password": "a-long-test-password"}).status_code == 200
        assert second.get("/files").json() == []
        assert first.get("/files").json()[0]["id"] == file_id


def test_jaffle_shop_tables_are_listed_for_an_authenticated_user():
    with TestClient(app) as client:
        assert client.post("/auth/register", json={"email": "sample@example.com", "password": "a-long-test-password"}).status_code == 200
        response = client.get("/sample-tables/jaffle-shop")
        assert response.status_code == 200
        tables = response.json()
        assert {"customers", "orders", "raw_payments"} <= {table["name"] for table in tables}
        assert all(table["columns"] for table in tables)


def test_auth_validation_returns_actionable_json():
    with TestClient(app) as client:
        response = client.post("/auth/register", json={"email": "bad", "password": "short"})
        assert response.status_code == 422
        assert response.json()["code"] == "VALIDATION_FAILED"
        assert response.json()["action"]


def test_reports_can_be_filtered_to_one_chat():
    with TestClient(app) as client:
        assert client.post("/auth/register", json={"email": "reports@example.com", "password": "a-long-test-password"}).status_code == 200
        first_chat = client.post("/chats", json={"title": "First"}).json()["id"]
        second_chat = client.post("/chats", json={"title": "Second"}).json()["id"]
        blocks = [{"type": "markdown", "content": {"text": "Summary"}}]
        first_report = client.post("/reports", json={"title": "First report", "chat_id": first_chat, "blocks": blocks})
        assert first_report.status_code == 200
        assert client.post("/reports", json={"title": "Second report", "chat_id": second_chat, "blocks": blocks}).status_code == 200
        assert [item["title"] for item in client.get(f"/reports?chat_id={first_chat}").json()] == ["First report"]
        assert {item["title"] for item in client.get("/reports").json()} == {"First report", "Second report"}


def test_run_trace_and_eval_suite_are_user_scoped():
    with TestClient(app) as client:
        user = client.post("/auth/register", json={"email": "trace@example.com", "password": "a-long-test-password"}).json()
        chat_id = client.post("/chats", json={"title": "Trace"}).json()["id"]
        with SessionLocal() as db:
            run = AgentRun(user_id=user["id"], chat_id=chat_id, prompt="profile data", resources={"file_ids": [], "connection_ids": []}, plan=[], model="test-model")
            db.add(run); db.commit(); db.refresh(run); run_id = run.id
        assert client.get(f"/runs/{run_id}").status_code == 200
        assert client.post(f"/runs/{run_id}/replay", json={}).status_code == 422
        suite = client.post("/eval-suites", json={"name": "Regression"})
        assert suite.status_code == 200
        assert client.get("/eval-suites").json()[0]["name"] == "Regression"


def test_eval_fixture_import_is_validated_and_idempotent():
    with TestClient(app) as client:
        user = client.post("/auth/register", json={"email": "fixture-api@example.com", "password": "a-long-test-password"}).json()
        with SessionLocal() as db:
            source = DataSource(
                user_id=user["id"],
                name="Northwind",
                host="localhost",
                database_name="northwind",
                username="reader",
                encrypted_password="not-used",
            )
            db.add(source)
            db.commit()
            db.refresh(source)
            source_id = source.id
        fixture = {
            "schema_version": 1,
            "suite": {"name": "Tiny fixture", "target_type": "connection", "dataset": {"sha256": "abc"}},
            "cases": [{
                "case_key": "T001",
                "name": "Count rows",
                "category": "smoke",
                "difficulty": "easy",
                "prompt": "Count rows",
                "reference_sql": "SELECT 1 AS value",
                "expected_tools": ["execute_sql"],
                "expect_report": False,
                "acceptance": {
                    "data_match": "exact_rows_ignore_order",
                    "required_tools": ["execute_sql"],
                    "expect_report": False,
                    "evidence_required": True,
                    "max_tool_calls": 2,
                },
            }],
        }

        first = client.post("/eval-suites/import", json={"target_id": source_id, "fixture": fixture})
        second = client.post("/eval-suites/import", json={"target_id": source_id, "fixture": fixture})

        assert first.status_code == 200
        assert first.json()["case_count"] == 1
        assert first.json()["imported"] is True
        assert second.json()["id"] == first.json()["id"]
        assert second.json()["imported"] is False
        detail = client.get(f"/eval-suites/{first.json()['id']}").json()
        assert detail["cases"][0]["case_key"] == "T001"
        assert detail["cases"][0]["acceptance"]["max_tool_calls"] == 2
