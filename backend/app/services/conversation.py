import json
from datetime import datetime
from sqlalchemy import desc
from sqlalchemy.orm import Session
from ..models import ChatCompaction, Message, MessagePart


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
        parts = db.query(MessagePart).filter(MessagePart.message_id == message.id).order_by(MessagePart.part_order).all()
        text_parts = [part.text for part in parts if part.part_type == "text" and part.text]
        tool_parts = [part for part in parts if part.part_type == "tool"]
        if not parts:  # Backward compatibility for messages written before structured parts.
            text_parts = [message.content]
            for i, event in enumerate(message.tool_events or []):
                tool_parts.append(_legacy_part(event, i))
        content = "\n".join(text_parts)
        if tool_parts:
            content += "\n" + "\n".join(_render_tool(part) for part in tool_parts)
        if content.strip():
            history.append({"role": message.role, "content": content.strip()})
    return history


def save_parts(db: Session, message: Message, content: str, tool_events: list[dict] | None = None) -> None:
    db.add(MessagePart(message_id=message.id, part_order=0, part_type="text", text=content))
    for index, event in enumerate(tool_events or [], start=1):
        output = event.get("output") if isinstance(event.get("output"), dict) else {"value": event.get("output")}
        db.add(MessagePart(message_id=message.id, part_order=index, part_type="tool", state="output", tool_name=str(event.get("name", "unknown")), input=event.get("input") if isinstance(event.get("input"), dict) else {}, output=output))


def _legacy_part(event: dict, index: int):
    class Part: pass
    part = Part()
    part.tool_name = str(event.get("name", "unknown"))
    part.state = "output"
    part.input = event.get("input") if isinstance(event.get("input"), dict) else {}
    part.output = event.get("output") if isinstance(event.get("output"), dict) else {"value": event.get("output")}
    part.error_text = None
    part.part_order = index
    return part


def _render_tool(part) -> str:
    if part.state == "error":
        return f"[tool {part.tool_name} failed: {part.error_text}]"
    output = dict(part.output or {})
    # Earlier results that share a query id are represented compactly.
    if output.get("superseded"):
        return f"[tool {part.tool_name}: result superseded]"
    rendered = json.dumps(output, ensure_ascii=False, default=str)
    return f"[tool {part.tool_name} input={json.dumps(part.input or {}, ensure_ascii=False)} output={rendered[:4000]}]"
