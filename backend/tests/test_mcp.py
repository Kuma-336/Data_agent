from fastapi.testclient import TestClient

from app.config import settings
from app.db import Base, engine
from app.main import app


def setup_function():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def test_mcp_is_opt_in_and_lists_governed_tools():
    previous = settings.mcp_enabled
    settings.mcp_enabled = True
    try:
        with TestClient(app) as client:
            assert client.post("/auth/register", json={"email": "mcp@example.com", "password": "a-long-test-password"}).status_code == 200
            token_response = client.post("/mcp/tokens")
            assert token_response.status_code == 200
            headers = {"Authorization": f"Bearer {token_response.json()['token']}"}
            initialized = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}})
            assert initialized.status_code == 200
            assert initialized.json()["result"]["serverInfo"]["name"] == "personal-data-agent"
            listed = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            names = {tool["name"] for tool in listed.json()["result"]["tools"]}
            assert {"ask_data_agent", "execute_sql", "grep", "ls", "create_report", "archive_report"} <= names
    finally:
        settings.mcp_enabled = previous


def test_mcp_requires_bearer_token():
    with TestClient(app) as client:
        response = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        assert response.status_code == 404
