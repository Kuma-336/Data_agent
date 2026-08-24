from sqlalchemy.orm import Session
from ..models import Chat, Memory, Message


def compact_chat_if_needed(db: Session, chat: Chat, limit: int = 30) -> None:
    """Deterministic fallback compaction; an LLM worker can replace this summary later."""
    messages = db.query(Message).filter(Message.chat_id == chat.id).order_by(Message.created_at).all()
    if len(messages) <= limit:
        return
    older = messages[:-12]
    chat.compacted_summary = "\n".join(f"{m.role}: {m.content[:400]}" for m in older)[-10_000:]
    db.commit()


def extract_explicit_memories(db: Session, user_id: str, chat_id: str, text: str) -> None:
    """Safe v1 extraction: save only explicit 'remember' instructions without invoking another model."""
    marker = "remember that "
    if marker not in text.lower():
        return
    content = text.lower().split(marker, 1)[1].strip()
    if not content or any(word in content for word in ("password", "token", "api key", "secret")):
        return
    db.add(Memory(user_id=user_id, source_chat_id=chat_id, category="user_instruction", content=content[:1000]))
    db.commit()
