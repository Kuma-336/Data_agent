"""Tools that read a database's persisted context documents on demand."""

from .. import context_store
from ..context_store import DOCUMENT_KINDS
from .registry import ToolContext, string, tool

_UNKNOWN_TARGET = {"error": "Unknown target; use `data`, `jaffle_shop`, or an attached connection id"}


@tool(
    name="list_table_context",
    description="List the tables of one database that have persisted context documents.",
    properties={"target": string("`data` for the attached file, `jaffle_shop` for the sample tables, otherwise a saved connection id.")},
    required=["target"],
)
def list_table_context(args: dict, context: ToolContext) -> dict:
    target = context.context_target(args["target"])
    if not target:
        return _UNKNOWN_TARGET
    return {"target": target.reference, "tables": context_store.list_tables(target)}


@tool(
    name="read_table_context",
    description=(
        "Read one persisted context document for a table. Use `columns` for the schema before naming "
        "any column, and `profiling` for the values actually present before filtering on any column value."
    ),
    properties={"target": string(), "table": string(), "kind": string(enum=list(DOCUMENT_KINDS))},
    required=["target", "table", "kind"],
)
def read_table_context(args: dict, context: ToolContext) -> dict:
    target = context.context_target(args["target"])
    if not target:
        return _UNKNOWN_TARGET
    content = context_store.read_table_document(target, args["table"], args["kind"])
    if content is None:
        return {"error": f"No {args['kind']} document for table {args['table']}; call list_table_context to see what exists"}
    return {"target": target.reference, "table": args["table"], "kind": args["kind"], "content": content}
