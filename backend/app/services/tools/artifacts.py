"""Tools that turn a query result into a chart or a downloadable file."""

from ..analytics import create_chart, export_csv
from .registry import ToolContext, array, string, tool


@tool(
    name="display_chart",
    description="Create a chart from a prior query result. Supported types: bar, line, pie, scatter, area, table.",
    properties={"query_id": string(), "title": string(), "type": string(), "x_key": string(), "y_keys": array()},
    required=["query_id", "type", "x_key", "y_keys"],
    risk="write",
)
def display_chart(args: dict, context: ToolContext) -> dict:
    chart = create_chart(context.db, context.user, context.chat.id, args["query_id"], args)
    return {"chart_id": chart.id, "spec": chart.spec}


@tool(
    name="export_result",
    description="Export a query result as CSV for download.",
    properties={"query_id": string()},
    required=["query_id"],
    risk="write",
)
def export_result(args: dict, context: ToolContext) -> dict:
    artifact = export_csv(context.db, context.user, context.chat.id, args["query_id"])
    return {"export_id": artifact.id, "format": "csv"}
