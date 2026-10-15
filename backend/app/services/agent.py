"""The agent loop: assemble context, let the model call tools, return an answer."""

from __future__ import annotations

import json
from collections.abc import Callable
from time import perf_counter

from openai import OpenAI
from sqlalchemy.orm import Session

from ..config import settings
from ..models import AgentRun, Chat, DataSource, ProjectContextDocument, UploadedFile, User
from .conversation import load_model_history
from .jaffle_shop import list_tables as list_jaffle_shop_tables
from .memory_manager import active_memories
from .prompt import build_system_prompt, database_context
from .runs import add_step, record_usage
from .tools import ToolContext, registry

ToolEvent = dict[str, object]

MAX_ITERATIONS = 8


def run_agent(
    db: Session,
    user: User,
    chat: Chat,
    user_text: str,
    file_ids: list[str],
    connection_ids: list[str],
    emit: Callable[[str, dict], None],
    run: AgentRun | None = None,
    jaffle_shop_table_names: list[str] | None = None,
) -> tuple[str, list[ToolEvent]]:
    files, sources, sample_tables = _resolve_resources(db, user, file_ids, connection_ids, jaffle_shop_table_names or [])
    context = ToolContext(
        db=db,
        user=user,
        chat=chat,
        files=files,
        sources=sources,
        jaffle_shop_tables=jaffle_shop_table_names or [],
    )
    messages = _initial_messages(db, user, chat, user_text, files, sources, sample_tables)
    client = OpenAI(base_url=settings.llm_base_url, api_key=settings.llm_api_key)
    definitions = registry.definitions()
    events: list[ToolEvent] = []

    for _ in range(MAX_ITERATIONS):
        started = perf_counter()
        response = client.chat.completions.create(model=settings.llm_model, messages=messages, tools=definitions, tool_choice="auto")
        _record_model_step(db, run, response.usage, len(events), len(definitions), started)
        message = response.choices[0].message
        if not message.tool_calls:
            return message.content or "I could not produce an answer.", events
        messages.append(message.model_dump(exclude_none=True))
        for call in message.tool_calls:
            args = json.loads(call.function.arguments)
            result = _call_tool(db, run, context, call.function.name, args, events, emit)
            messages.append({"role": "tool", "tool_call_id": call.id, "content": json.dumps(result, ensure_ascii=False, default=str)})
            if registry.ends_turn(call.function.name):
                return "", events
    return "I stopped after the maximum number of tool calls.", events


def _resolve_resources(
    db: Session, user: User, file_ids: list[str], connection_ids: list[str], jaffle_shop_table_names: list[str]
) -> tuple[list[UploadedFile], list[DataSource], list[dict]]:
    """Load every attached resource under the user's own scope; a miss fails the turn."""
    files = db.query(UploadedFile).filter(UploadedFile.user_id == user.id, UploadedFile.id.in_(file_ids)).all() if file_ids else []
    sources = db.query(DataSource).filter(DataSource.user_id == user.id, DataSource.id.in_(connection_ids)).all() if connection_ids else []
    if len(files) != len(file_ids) or len(sources) != len(connection_ids):
        raise ValueError("One or more selected data resources are not available")
    if not jaffle_shop_table_names:
        return files, sources, []
    available = {item["name"]: item for item in list_jaffle_shop_tables()}
    if any(name not in available for name in jaffle_shop_table_names):
        raise ValueError("One or more selected jaffle_shop tables are not available")
    return files, sources, [available[name] for name in jaffle_shop_table_names]


def _initial_messages(
    db: Session,
    user: User,
    chat: Chat,
    user_text: str,
    files: list[UploadedFile],
    sources: list[DataSource],
    sample_tables: list[dict],
) -> list[dict]:
    memories = active_memories(db, user.id, chat.id)
    context_docs = db.query(ProjectContextDocument).filter(ProjectContextDocument.user_id == user.id).all()
    databases = database_context(user, files, sources, bool(sample_tables))
    messages: list[dict] = [{"role": "system", "content": build_system_prompt(memories, files, sources, context_docs, sample_tables, databases)}]
    if chat.compacted_summary:
        messages.append({"role": "system", "content": f"Earlier conversation summary: {chat.compacted_summary}"})
    messages.extend(load_model_history(db, chat.id))
    messages.append({"role": "user", "content": user_text})
    return messages


def _call_tool(
    db: Session,
    run: AgentRun | None,
    context: ToolContext,
    name: str,
    args: dict,
    events: list[ToolEvent],
    emit: Callable[[str, dict], None],
) -> dict:
    started = perf_counter()
    result = registry.dispatch(name, args, context)
    event = {"name": name, "input": args, "output": result}
    events.append(event)
    if run:
        error = str(result.get("error")) if isinstance(result, dict) and result.get("error") else None
        add_step(db, run, "tool", name, args, result, error=error, duration_ms=_elapsed_ms(started))
    emit("tool", event)
    return result


def _record_model_step(db: Session, run: AgentRun | None, usage: object, iteration: int, tool_count: int, started: float) -> None:
    if not run:
        return
    input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
    output_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
    record_usage(db, run, input_tokens, output_tokens)
    add_step(
        db,
        run,
        "model",
        "chat_completion",
        {"iteration": iteration, "tool_count": tool_count},
        {"input_tokens": input_tokens, "output_tokens": output_tokens},
        duration_ms=_elapsed_ms(started),
    )


def _elapsed_ms(started: float) -> int:
    return int((perf_counter() - started) * 1000)
