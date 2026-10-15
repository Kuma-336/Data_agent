"""Tools that describe an attached resource before it is queried."""

from .. import context_store
from ..analytics import profile_file
from .registry import ToolContext, string, tool


@tool(
    name="inspect_file",
    description="Inspect an uploaded CSV/XLSX before analysing it.",
    properties={"file_id": string()},
    required=["file_id"],
)
def inspect_file(args: dict, context: ToolContext) -> dict:
    file = context.file(args["file_id"])
    return file.file_schema if file else {"error": "File is not attached to this message"}


@tool(
    name="inspect_database",
    description=(
        "Show the table index of one saved PostgreSQL connection. Read a table's `columns` "
        "document with read_table_context before naming its columns."
    ),
    properties={"connection_id": string()},
    required=["connection_id"],
)
def inspect_database(args: dict, context: ToolContext) -> dict:
    source = context.source(args["connection_id"])
    if not source:
        return {"error": "Connection is not attached"}
    tables = context_store.list_tables(context_store.connection_target(source))
    if tables:
        return {
            "connection_id": source.id,
            "database": source.database_name,
            "tables": tables,
            "hint": "Read a table's columns document with read_table_context before naming its columns.",
        }
    return source.schema_cache or {"error": "Connection has no synced context yet; ask the user to sync it before relying on its schema"}


@tool(
    name="profile_data",
    description="Profile an attached file before analysis: fields, types, nulls, duplicates, and numeric ranges.",
    properties={"file_id": string()},
    required=["file_id"],
)
def profile_data(args: dict, context: ToolContext) -> dict:
    file = context.file(args["file_id"])
    if not file:
        return {"error": "File is not attached"}
    return profile_file(context.db, context.user, file).profile
