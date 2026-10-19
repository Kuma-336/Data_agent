"""Persist a turn's parts and rebuild the history the model sees.

Tool calls are replayed as the protocol's own `tool_calls` plus `role: "tool"`
messages, not flattened into the assistant's prose. Flattening them made "a
tool ran and returned this" indistinguishable from "the assistant wrote text
that looks like a tool result", and the model started writing the latter
instead of calling tools.
"""

import json
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import desc
from sqlalchemy.orm import Session

from ..models import ChatCompaction, Message, MessagePart

HISTORY_TOOL_OUTPUT_CHARS = 4000


def settle_interrupted_parts(db: Session, chat_id: str) -> None:
    """Persist a terminal error for tool calls that survived a server restart mid-run."""
    parts = db.query(MessagePart).join(Message).filter(Message.chat_id == chat_id, MessagePart.part_type == "tool", MessagePart.state == "running").all()
    for part in parts:
        part.state = "error"
        part.error_text = "Tool call interrupted before completion. Retry the request."
    if parts:
        db.commit()


def latest_compaction(db: Session, chat_id: str) -> ChatCompaction | None:
    return db.query(ChatCompaction).filter(ChatCompaction.chat_id == chat_id).order_by(desc(ChatCompaction.created_at)).first()


def load_model_history(db: Session, chat_id: str) -> list[dict]:
    """Rebuild text plus persisted tool results after the latest compaction boundary."""
    settle_interrupted_parts(db, chat_id)
    compaction = latest_compaction(db, chat_id)
    messages = db.query(Message).filter(Message.chat_id == chat_id, Message.superseded_at.is_(None)).order_by(Message.created_at).all()
    if compaction:
        index = next((i for i, message in enumerate(messages) if message.id == compaction.through_message_id), -1)
        messages = messages[index + 1 :] if index >= 0 else messages
    history: list[dict] = []
    if compaction:
        history.append({"role": "assistant", "content": f"<conversation-summary>\n{compaction.summary}\n</conversation-summary>"})
    for message in messages:
        history.extend(_render_message(db, message))
    return history


def save_parts(db: Session, message: Message, content: str, tool_events: list[dict] | None = None) -> None:
    db.add(MessagePart(message_id=message.id, part_order=0, part_type="text", text=content))
    for index, event in enumerate(tool_events or [], start=1):
        output = event.get("output") if isinstance(event.get("output"), dict) else {"value": event.get("output")}
        db.add(
            MessagePart(
                message_id=message.id,
                part_order=index,
                part_type="tool",
                state="output",
                tool_name=str(event.get("name", "unknown")),
                tool_call_id=event.get("tool_call_id"),
                input=event.get("input") if isinstance(event.get("input"), dict) else {},
                output=output,
            )
        )


def _render_message(db: Session, message: Message) -> list[dict]:
    parts = db.query(MessagePart).filter(MessagePart.message_id == message.id).order_by(MessagePart.part_order).all()
    if parts:
        text = "\n".join(part.text for part in parts if part.part_type == "text" and part.text).strip()
        tool_parts = [part for part in parts if part.part_type == "tool"]
    else:  # Backward compatibility for messages written before structured parts.
        text = (message.content or "").strip()
        tool_parts = [_legacy_part(event, index) for index, event in enumerate(message.tool_events or [])]
    if not tool_parts:
        return [{"role": message.role, "content": text}] if text else []
    calls = [_tool_call(part) for part in tool_parts]
    results = [{"role": "tool", "tool_call_id": call["id"], "content": _tool_output(part)} for call, part in zip(calls, tool_parts, strict=True)]
    return [{"role": "assistant", "content": text, "tool_calls": calls}, *results]


def _tool_call(part) -> dict:
    return {
        "id": _tool_call_id(part),
        "type": "function",
        "function": {"name": part.tool_name or "unknown", "arguments": json.dumps(part.input or {}, ensure_ascii=False, default=str)},
    }


def _tool_call_id(part) -> str:
    """Rows written before tool_call_id was persisted still need a unique id to pair with."""
    return part.tool_call_id or f"call_{part.id}"


def _tool_output(part) -> str:
    if part.state == "error":
        return f"Tool failed: {part.error_text}"
    output = dict(part.output or {})
    # Earlier results that share a query id are represented compactly.
    if output.get("superseded"):
        return "Result superseded by a later call."
    rendered = json.dumps(output, ensure_ascii=False, default=str)
    if len(rendered) > HISTORY_TOOL_OUTPUT_CHARS:
        return rendered[:HISTORY_TOOL_OUTPUT_CHARS] + "… (truncated in history)"
    return rendered


@dataclass
class _LegacyPart:
    id: str
    tool_name: str
    state: str
    input: dict
    output: dict
    part_order: int
    tool_call_id: str | None = None
    error_text: str | None = None
    text: str | None = None
    part_type: str = "tool"


def _legacy_part(event: dict, index: int) -> _LegacyPart:
    return _LegacyPart(
        id=f"legacy{index}",
        tool_name=str(event.get("name", "unknown")),
        state="output",
        input=event.get("input") if isinstance(event.get("input"), dict) else {},
        output=event.get("output") if isinstance(event.get("output"), dict) else {"value": event.get("output")},
        part_order=index,
    )
