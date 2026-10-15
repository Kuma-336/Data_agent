"""Tools that read user-authored business context and analysis playbooks."""

from ...models import ProjectContextDocument
from ..skills import load_skill as read_skill
from .registry import ToolContext, string, tool


@tool(
    name="list_context",
    description="List user business-rule and data-dictionary documents.",
)
def list_context(args: dict, context: ToolContext) -> dict:
    documents = (
        context.db.query(ProjectContextDocument)
        .filter(ProjectContextDocument.user_id == context.user.id)
        .all()
    )
    return {"documents": [item.name for item in documents]}


@tool(
    name="read_context",
    description="Read one business-rule or data-dictionary document by name.",
    properties={"name": string()},
    required=["name"],
)
def read_context(args: dict, context: ToolContext) -> dict:
    document = (
        context.db.query(ProjectContextDocument)
        .filter(ProjectContextDocument.user_id == context.user.id, ProjectContextDocument.name == args["name"])
        .first()
    )
    return {"name": document.name, "content": document.content} if document else {"error": "Context document not found"}


@tool(
    name="load_skill",
    description="Load an analysis playbook: excel-handling, data-profiling, postgres-analysis, reporting.",
    properties={"name": string()},
    required=["name"],
)
def load_skill(args: dict, context: ToolContext) -> dict:
    content = read_skill(args["name"])
    return {"name": args["name"], "content": content} if content else {"error": "Unknown skill"}
