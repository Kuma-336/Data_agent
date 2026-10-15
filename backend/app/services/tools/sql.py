"""Guarded read-only SQL execution and paged access to earlier results."""

from ...config import settings
from ...models import QueryResult
from ..query import execute_jaffle_shop, execute_local, execute_postgres
from .registry import ToolContext, integer, string, tool

PREVIEW_ROWS = 20


@tool(
    name="execute_sql",
    description=(
        "Execute a single read-only SELECT/WITH query. Use `data` for an attached local file, "
        "`jaffle_shop` for selected sample tables, otherwise use a saved connection id."
    ),
    properties={"target": string(), "sql": string()},
    required=["target", "sql"],
)
def execute_sql(args: dict, context: ToolContext) -> dict:
    """Returns a handle plus a short preview; the full rows stay in the database."""
    result = _run(args["target"], args["sql"], context)
    if isinstance(result, dict):
        return result
    return {
        "query_id": result.id,
        "columns": result.columns,
        "row_count": len(result.rows),
        "preview": result.rows[:PREVIEW_ROWS],
        "sql": result.sql,
    }


@tool(
    name="read_query_result",
    description="Read a page from an earlier query result.",
    properties={"query_id": string(), "offset": integer(), "limit": integer()},
    required=["query_id"],
)
def read_query_result(args: dict, context: ToolContext) -> dict:
    result = (
        context.db.query(QueryResult)
        .filter(QueryResult.id == args["query_id"], QueryResult.user_id == context.user.id)
        .first()
    )
    if not result:
        return {"error": "Query result not found"}
    offset = args.get("offset", 0)
    limit = min(args.get("limit", 100), settings.max_query_rows)
    return {"query_id": result.id, "columns": result.columns, "rows": result.rows[offset : offset + limit]}


def _run(target: str, sql: str, context: ToolContext) -> QueryResult | dict:
    if target == "data":
        if len(context.files) != 1:
            return {"error": "Attach exactly one local file before using target data"}
        return execute_local(context.db, context.user, context.chat.id, context.files[0], sql)
    if target == "jaffle_shop":
        return execute_jaffle_shop(context.db, context.user, context.chat.id, context.jaffle_shop_tables, sql)
    source = context.source(target)
    if not source:
        return {"error": "Connection is not attached"}
    return execute_postgres(context.db, context.user, context.chat.id, source, sql)
