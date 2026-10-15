"""Tool contract and registry for the agent loop.

A tool declares its schema and handler in one place; the registry owns the
cross-cutting concerns the loop should not repeat per tool: resource lookup
scoped to the turn, uniform error shape, and whether a call ends the turn.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ...models import Chat, DataSource, UploadedFile, User
from .. import context_store
from ..context_store import ContextTarget


@dataclass(frozen=True)
class ToolContext:
    """Everything a tool may touch, already scoped to the authenticated user and turn."""

    db: Session
    user: User
    chat: Chat
    files: list[UploadedFile] = field(default_factory=list)
    sources: list[DataSource] = field(default_factory=list)
    jaffle_shop_tables: list[str] = field(default_factory=list)

    def file(self, file_id: str) -> UploadedFile | None:
        return next((item for item in self.files if item.id == file_id), None)

    def source(self, source_id: str) -> DataSource | None:
        return next((item for item in self.sources if item.id == source_id), None)

    def context_target(self, target: str) -> ContextTarget | None:
        """Resolve a tool `target` to a context handle, only from resources attached this turn."""
        if target == "data":
            return context_store.file_target(self.files[0]) if len(self.files) == 1 else None
        if target == "jaffle_shop":
            return context_store.sample_target(self.user.id)
        source = self.source(target)
        if source:
            return context_store.connection_target(source)
        file = self.file(target)
        return context_store.file_target(file) if file else None


ToolHandler = Callable[[dict, ToolContext], dict]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict
    handler: ToolHandler
    ends_turn: bool = False

    @property
    def definition(self) -> dict:
        return {"type": "function", "function": {"name": self.name, "description": self.description, "parameters": self.parameters}}


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def definitions(self) -> list[dict]:
        return [tool.definition for tool in self._tools.values()]

    def dispatch(self, name: str, args: dict, context: ToolContext) -> dict:
        """Run one tool, turning an HTTPException into the error shape the model can act on."""
        tool = self._tools.get(name)
        if not tool:
            return {"error": f"Unknown tool {name}"}
        try:
            return tool.handler(args, context)
        except HTTPException as exc:
            return {"error": str(exc.detail), "retryable": True}

    def ends_turn(self, name: str) -> bool:
        tool = self._tools.get(name)
        return bool(tool and tool.ends_turn)

    def register(self, tool: Tool) -> Tool:
        if tool.name in self._tools:
            raise ValueError(f"Tool '{tool.name}' is already registered")
        self._tools[tool.name] = tool
        return tool

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: object) -> bool:
        return name in self._tools


registry = ToolRegistry()


def tool(*, name: str, description: str, properties: dict | None = None, required: list[str] | None = None, ends_turn: bool = False):
    """Declare a tool and register it. The decorated function stays directly callable."""

    def decorate(handler: ToolHandler) -> ToolHandler:
        parameters = {"type": "object", "properties": properties or {}}
        if required:
            parameters["required"] = required
        registry.register(Tool(name=name, description=description, parameters=parameters, handler=handler, ends_turn=ends_turn))
        return handler

    return decorate


def string(description: str | None = None, enum: list[str] | None = None) -> dict:
    schema: dict = {"type": "string"}
    if description:
        schema["description"] = description
    if enum:
        schema["enum"] = enum
    return schema


def integer() -> dict:
    return {"type": "integer"}


def array(item_type: str = "string") -> dict:
    return {"type": "array", "items": {"type": item_type}}
