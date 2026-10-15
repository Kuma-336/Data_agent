import pytest
from fastapi import HTTPException

from app.models import Chat, DataSource, UploadedFile, User
from app.services.tools import ToolContext, registry
from app.services.tools.registry import Tool, ToolRegistry

EXPECTED_TOOLS = {
    "inspect_file", "inspect_database", "profile_data", "list_table_context", "read_table_context",
    "execute_sql", "read_query_result", "clarify", "display_chart", "export_result",
    "list_context", "read_context", "load_skill", "create_report", "list_reports",
}


def _context(**overrides) -> ToolContext:
    defaults = {"db": None, "user": User(id="user-1"), "chat": Chat(id="chat-1")}
    return ToolContext(**(defaults | overrides))


def test_every_tool_is_registered_with_a_valid_function_schema():
    definitions = registry.definitions()
    assert {item["function"]["name"] for item in definitions} == EXPECTED_TOOLS
    for item in definitions:
        assert item["type"] == "function"
        function = item["function"]
        assert function["description"].strip()
        assert function["parameters"]["type"] == "object"
        for name in function["parameters"].get("required", []):
            assert name in function["parameters"]["properties"], f"{function['name']} requires undeclared {name}"


def test_only_clarify_ends_the_turn():
    ending = {name for name in EXPECTED_TOOLS if registry.ends_turn(name)}
    assert ending == {"clarify"}


def test_unknown_tool_returns_an_error_instead_of_raising():
    assert registry.dispatch("no_such_tool", {}, _context()) == {"error": "Unknown tool no_such_tool"}


def test_dispatch_converts_an_http_exception_into_a_retryable_error():
    local = ToolRegistry()

    def failing(args: dict, context: ToolContext) -> dict:
        raise HTTPException(400, "Local query failed: no such column")

    local.register(Tool(name="boom", description="d", parameters={"type": "object", "properties": {}}, handler=failing))
    assert local.dispatch("boom", {}, _context()) == {"error": "Local query failed: no such column", "retryable": True}


def test_dispatch_lets_unexpected_errors_surface():
    local = ToolRegistry()

    def failing(args: dict, context: ToolContext) -> dict:
        raise KeyError("target")

    local.register(Tool(name="boom", description="d", parameters={"type": "object", "properties": {}}, handler=failing))
    with pytest.raises(KeyError):
        local.dispatch("boom", {}, _context())


def test_registering_a_duplicate_name_is_rejected():
    local = ToolRegistry()
    spec = Tool(name="dup", description="d", parameters={"type": "object", "properties": {}}, handler=lambda args, context: {})
    local.register(spec)
    with pytest.raises(ValueError):
        local.register(spec)


def test_context_target_resolves_only_resources_attached_to_the_turn():
    file = UploadedFile(id="file-1", user_id="user-1", original_name="x.csv", storage_path="", media_type="text/csv", size_bytes=0)
    source = DataSource(id="conn-1", user_id="user-1", name="Warehouse", host="localhost", port=5432, database_name="northwind", username="r", encrypted_password="x", sslmode="prefer")
    context = _context(files=[file], sources=[source])

    assert context.context_target("data").reference == "file:file-1"
    assert context.context_target("conn-1").reference == "conn-1"
    assert context.context_target("file-1").reference == "file:file-1"
    assert context.context_target("jaffle_shop").reference == "sample:jaffle_shop"
    assert context.context_target("conn-unattached") is None

    # `data` is ambiguous unless exactly one file is attached.
    assert _context(files=[file, file]).context_target("data") is None
    assert _context().context_target("data") is None
