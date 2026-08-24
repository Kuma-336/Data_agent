from app.db import Base, SessionLocal, engine
from app.models import Chat, ChatCompaction, Memory, Message, User
from app.services.conversation import load_model_history, save_parts
from app.services.memory_manager import active_memories


def setup_function():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def test_history_restores_text_and_tool_output_after_compaction():
    with SessionLocal() as db:
        user = User(email="history@example.com", password_hash="unused")
        db.add(user); db.flush()
        chat = Chat(user_id=user.id, title="history")
        db.add(chat); db.flush()
        first = Message(chat_id=chat.id, role="user", content="old question")
        db.add(first); db.flush(); save_parts(db, first, first.content)
        latest = Message(chat_id=chat.id, role="assistant", content="latest answer", tool_events=[{"name": "execute_sql", "input": {"sql": "SELECT 1"}, "output": {"query_id": "query_1", "preview": [{"n": 1}]}}])
        db.add(latest); db.flush(); save_parts(db, latest, latest.content, latest.tool_events)
        db.add(ChatCompaction(chat_id=chat.id, through_message_id=first.id, summary="old question was resolved", estimated_tokens=100, model="test"))
        db.commit()
        history = load_model_history(db, chat.id)
        assert "conversation-summary" in history[0]["content"]
        assert history[1]["content"].startswith("latest answer")
        assert "query_1" in history[1]["content"]


def test_superseded_memory_is_not_injected():
    with SessionLocal() as db:
        user = User(email="memory@example.com", password_hash="unused")
        db.add(user); db.flush()
        old = Memory(user_id=user.id, category="user_instruction", content="Use monthly output", superseded=True)
        new = Memory(user_id=user.id, category="user_instruction", content="Use weekly output")
        db.add_all([old, new]); db.flush(); old.superseded_by = new.id; db.commit()
        assert [item.content for item in active_memories(db, user.id)] == ["Use weekly output"]
