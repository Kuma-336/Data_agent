"""Tools that hand control back to the user."""

from .registry import ToolContext, array, string, tool


@tool(
    name="clarify",
    description="Ask one focused question when needed data or intent is missing.",
    properties={"question": string(), "options": array()},
    required=["question"],
    ends_turn=True,
)
def clarify(args: dict, context: ToolContext) -> dict:
    return {"question": args["question"], "options": args.get("options", [])}
