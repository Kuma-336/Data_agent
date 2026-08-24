import dramatiq
from dramatiq.brokers.redis import RedisBroker
from .config import settings
from .db import SessionLocal
from .models import Chat
from .services.memory_manager import extract_memories, maybe_compact
from .services.agent import TOOL_DEFINITIONS

dramatiq.set_broker(RedisBroker(url=settings.redis_url))


@dramatiq.actor
def extract_memory(user_id: str, chat_id: str, source_message_id: str | None = None) -> None:
    with SessionLocal() as db:
        extract_memories(db, user_id, chat_id, source_message_id)


@dramatiq.actor
def compact_chat(chat_id: str) -> None:
    with SessionLocal() as db:
        maybe_compact(db, chat_id, TOOL_DEFINITIONS)
