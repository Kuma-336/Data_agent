import json
import tiktoken
from openai import OpenAI
from sqlalchemy import desc
from sqlalchemy.orm import Session
from ..config import settings
from ..models import Chat, ChatCompaction, Memory, Message
from .conversation import load_model_history

TOKEN_ENCODING = tiktoken.get_encoding("cl100k_base")


def active_memories(db: Session, user_id: str, exclude_chat_id: str | None = None) -> list[Memory]:
    query = db.query(Memory).filter(Memory.user_id == user_id, Memory.superseded.is_(False), Memory.superseded_by.is_(None))
    if exclude_chat_id:
        query = query.filter((Memory.source_chat_id != exclude_chat_id) | (Memory.source_chat_id.is_(None)))
    return query.order_by(desc(Memory.updated_at), desc(Memory.created_at)).all()


def estimate_tokens(messages: list[dict], tool_schema: list[dict]) -> int:
    payload = json.dumps({"messages": messages, "tools": tool_schema}, ensure_ascii=False, default=str)
    return int(len(TOKEN_ENCODING.encode(payload)) * 1.1) + settings.llm_max_output_tokens


def maybe_compact(db: Session, chat_id: str, tool_schema: list[dict]) -> None:
    chat = db.get(Chat, chat_id)
    if not chat:
        return
    messages = load_model_history(db, chat_id)
    estimate = estimate_tokens(messages, tool_schema)
    if estimate < int(settings.llm_context_window * 0.75) or len(messages) < 4:
        return
    # Preserve the latest user message and everything after it; summarize older history.
    last_user = max((i for i, message in enumerate(messages) if message["role"] == "user"), default=-1)
    if last_user < 1:
        return
    source = messages[:last_user]
    prompt = "Summarize this data-analysis conversation for future turns. Preserve user preferences, files, data definitions, executed SQL, query ids, decisions, and unresolved questions. Do not invent facts.\n\n" + json.dumps(source, ensure_ascii=False)
    client = OpenAI(base_url=settings.llm_base_url, api_key=settings.llm_api_key)
    response = client.chat.completions.create(model=settings.llm_model, messages=[{"role": "user", "content": prompt}], max_tokens=settings.llm_max_output_tokens)
    summary = response.choices[0].message.content or ""
    db_messages = db.query(Message).filter(Message.chat_id == chat_id).order_by(Message.created_at).all()
    through = db_messages[max(0, len(db_messages) - len(messages) + last_user - 1)]
    db.add(ChatCompaction(chat_id=chat_id, through_message_id=through.id, summary=summary, estimated_tokens=estimate, model=settings.llm_model))
    db.commit()


def extract_memories(db: Session, user_id: str, chat_id: str, source_message_id: str | None) -> None:
    chat = db.get(Chat, chat_id)
    if not chat:
        return
    from ..models import User
    user = db.get(User, user_id)
    if not user or not user.memory_enabled:
        return
    existing = active_memories(db, user_id)
    history = load_model_history(db, chat_id)[-17:]
    prompt = """Extract only durable user preferences, formatting rules, and stable business definitions from this conversation. Never store credentials, secrets, raw data rows, transient requests, or one-off conclusions. Return JSON only: {\"user_instructions\":[{\"content\":string,\"supersedes_id\":string|null}],\"user_profile\":[{\"content\":string,\"supersedes_id\":string|null}]}. supersedes_id must be an id from existing memories or null."""
    payload = {"existing_memories": [{"id": item.id, "category": item.category, "content": item.content} for item in existing], "conversation": history}
    client = OpenAI(base_url=settings.llm_base_url, api_key=settings.llm_api_key)
    response = client.chat.completions.create(model=settings.llm_model, messages=[{"role": "system", "content": prompt}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}], response_format={"type": "json_object"}, max_tokens=1000)
    data = json.loads(response.choices[0].message.content or "{}")
    valid_ids = {item.id for item in existing}
    for category, key in (("user_instruction", "user_instructions"), ("user_profile", "user_profile")):
        for item in data.get(key, []):
            content = " ".join(str(item.get("content", "")).split()).strip()
            if not content or len(content) > 1000 or any(secret in content.lower() for secret in ("api key", "password", "token", "secret", "connection string")):
                continue
            previous = item.get("supersedes_id") if item.get("supersedes_id") in valid_ids else None
            memory = Memory(user_id=user_id, source_chat_id=chat_id, source_message_id=source_message_id, category=category, content=content, superseded=False)
            db.add(memory)
            db.flush()
            if previous:
                old = db.get(Memory, previous)
                if old:
                    old.superseded = True
                    old.superseded_by = memory.id
    db.commit()
