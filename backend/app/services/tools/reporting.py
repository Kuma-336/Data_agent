"""Tools that persist findings as a private report."""

from ...models import Report
from ..reports import save_report
from .registry import ToolContext, string, tool


@tool(
    name="create_report",
    description=(
        "Create a private report from markdown, metric, query_table, chart, or divider blocks that "
        "reference existing query_id/chart_id values. Always include a markdown block first that gives "
        "a concise executive summary, findings, caveats, and recommended next actions; do not create a "
        "charts-only report."
    ),
    properties={"title": string(), "blocks": {"type": "array", "items": {"type": "object"}}},
    required=["title", "blocks"],
)
def create_report(args: dict, context: ToolContext) -> dict:
    report = save_report(context.db, context.user, args["title"], args["blocks"], context.chat.id)
    return {"report_id": report.id, "title": report.title}


@tool(
    name="list_reports",
    description="List the user's private reports.",
)
def list_reports(args: dict, context: ToolContext) -> dict:
    reports = context.db.query(Report).filter(Report.user_id == context.user.id).all()
    return {"reports": [{"id": item.id, "title": item.title} for item in reports]}
