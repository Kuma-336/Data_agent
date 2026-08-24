from io import BytesIO
from fastapi.testclient import TestClient
from app.db import Base, engine
from app.main import app


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


def test_auth_validation_returns_actionable_json():
    with TestClient(app) as client:
        response = client.post("/auth/register", json={"email": "bad", "password": "short"})
        assert response.status_code == 422
        assert response.json()["code"] == "VALIDATION_FAILED"
        assert response.json()["action"]
