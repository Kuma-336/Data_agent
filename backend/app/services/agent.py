"""The agent loop: assemble context, let the model call tools, return an answer."""

from __future__ import annotations

import json
from collections.abc import Iterator
from time import perf_counter

from openai import OpenAI
from sqlalchemy.orm import Session

from ..config import settings
from ..models import AgentRun, Chat, DataSource, ProjectContextDocument, UploadedFile, User
from .approvals import granted_fingerprints, record_request
from . import context_store
from .context_sync import sync_sample_context
from .conversation import load_model_history
from .jaffle_shop import list_tables as list_jaffle_shop_tables
from .memory_manager import active_memories
from .prompt import build_system_prompt, database_context
from .runs import add_step, record_usage
from .tools import Budget, HookDecision, ToolContext, registry, resolve_policy

ToolEvent = dict[str, object]


def run_agent(
    db: Session,
    user: User,
    chat: Chat,
    user_text: str,
    file_ids: list[str],
    connection_ids: list[str],
    run: AgentRun | None = None,
    jaffle_shop_table_names: list[str] | None = None,
) -> tuple[str, list[ToolEvent]]:
    """Drain the event stream for callers that only want the final result."""
    answer = ""
    events: list[ToolEvent] = []
    for kind, data in iter_agent(db, user, chat, user_text, file_ids, connection_ids, run, jaffle_shop_table_names):
        if kind == "tool":
            events.append(data)
        elif kind == "answer":
            answer = str(data["content"])
    return answer, events


def iter_agent(
    db: Session,
    user: User,
    chat: Chat,
    user_text: str,
    file_ids: list[str],
    connection_ids: list[str],
    run: AgentRun | None = None,
    jaffle_shop_table_names: list[str] | None = None,
) -> Iterator[tuple[str, dict]]:
    """Yield each tool event as it completes, then one terminal `answer` event.

    A generator rather than a callback plus a thread: the caller and the loop
    share one SQLAlchemy Session, which is not thread-safe.
    """
    files, sources, sample_tables = _resolve_resources(db, user, file_ids, connection_ids, jaffle_shop_table_names or [])
    policy = resolve_policy(db, user)
    context = ToolContext(
        db=db,
        user=user,
        chat=chat,
        files=files,
        sources=sources,
        jaffle_shop_tables=jaffle_shop_table_names or [],
        budget=Budget(policy=policy, approvals=granted_fingerprints(db, user.id, chat.id)),
    )
    if run:
        add_step(db, run, "policy", "tool_policy", {"resources": run.resources}, policy.as_dict())
    messages = _initial_messages(db, user, chat, user_text, files, sources, sample_tables)
    client = OpenAI(base_url=settings.llm_base_url, api_key=settings.llm_api_key, timeout=settings.llm_timeout_seconds)
    definitions = registry.definitions()
    events: list[ToolEvent] = []

    for _ in range(policy.max_iterations):
        started = perf_counter()
        response = client.chat.completions.create(model=settings.llm_model, messages=messages, tools=definitions, tool_choice="auto")
        _record_model_step(db, run, response.usage, len(events), len(definitions), started)
        message = response.choices[0].message
        if not message.tool_calls:
            yield "answer", {"content": message.content or "I could not produce an answer."}
            return
        messages.append(message.model_dump(exclude_none=True))
        for call in message.tool_calls:
            args = json.loads(call.function.arguments)
            result, event = _call_tool(db, run, context, call.function.name, args, call.id)
            events.append(event)
            yield "tool", event
            messages.append({"role": "tool", "tool_call_id": call.id, "content": json.dumps(result, ensure_ascii=False, default=str)})
            # A failed turn-ending call must not end the turn, or the user gets nothing back.
            if registry.ends_turn(call.function.name) and not result.get("error"):
                yield "answer", {"content": ""}
                return
    yield "answer", {"content": "I stopped after the maximum number of tool calls."}


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
    _ensure_sample_context(user)
    return files, sources, [available[name] for name in jaffle_shop_table_names]


def _ensure_sample_context(user: User) -> None:
    """Sync the sample database the first time a user attaches it.

    It is eight read-only tables in a local file, so it belongs in the request
    path; a warehouse connection does not, which is why that one stays an
    explicit endpoint. Without this the tables have no columns document and the
    model is told to read one that was never generated.
    """
    if context_store.list_tables(context_store.sample_target(user.id)):
        return
    try:
        sync_sample_context(user.id)
    except Exception:
        # Context is a read-time optimisation; never fail the turn over it.
        pass


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


def _call_tool(db: Session, run: AgentRun | None, context: ToolContext, name: str, args: dict, tool_call_id: str | None = None) -> tuple[dict, ToolEvent]:
    started = perf_counter()
    result = registry.dispatch(name, args, context)
    # Drained before the tool step so hook decisions order ahead of the call they shaped.
    _record_hook_decisions(db, run, context, name)
    if run:
        error = str(result.get("error")) if isinstance(result, dict) and result.get("error") else None
        add_step(db, run, "tool", name, args, result, error=error, duration_ms=_elapsed_ms(started))
    return result, {"name": name, "input": args, "output": result, "tool_call_id": tool_call_id}


def _record_hook_decisions(db: Session, run: AgentRun | None, context: ToolContext, tool_name: str) -> None:
    for decision in context.budget.drain():
        if decision.action == "approval_required":
            record_request(db, context.user, context.chat.id, run, tool_name, decision.detail["fingerprint"], decision.detail.get("args", {}))
        if run:
            add_step(
                db,
                run,
                "hook",
                decision.hook,
                {"tool": decision.tool, **_without_args(decision)},
                {"action": decision.action, "code": decision.code},
                error=decision.detail.get("reason") if decision.action == "deny" else None,
            )


def _without_args(decision: HookDecision) -> dict:
    return {key: value for key, value in decision.detail.items() if key != "args"}


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
