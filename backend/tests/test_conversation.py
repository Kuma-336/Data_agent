import json

from app.db import Base, SessionLocal, engine
from app.models import Chat, ChatCompaction, Memory, Message, MessagePart, User
from app.services.conversation import HISTORY_TOOL_OUTPUT_CHARS, load_model_history, save_parts
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
        # The assistant keeps only its prose; the call and its result are protocol messages.
        assert history[1] == {
            "role": "assistant",
            "content": "latest answer",
            "tool_calls": [{"id": history[1]["tool_calls"][0]["id"], "type": "function", "function": {"name": "execute_sql", "arguments": '{"sql": "SELECT 1"}'}}],
        }
        assert "query_1" not in history[1]["content"]
        assert history[2]["role"] == "tool"
        assert history[2]["tool_call_id"] == history[1]["tool_calls"][0]["id"]
        assert "query_1" in history[2]["content"]


def test_superseded_memory_is_not_injected():
    with SessionLocal() as db:
        user = User(email="memory@example.com", password_hash="unused")
        db.add(user); db.flush()
        old = Memory(user_id=user.id, category="user_instruction", content="Use monthly output", superseded=True)
        new = Memory(user_id=user.id, category="user_instruction", content="Use weekly output")
        db.add_all([old, new]); db.flush(); old.superseded_by = new.id; db.commit()
        assert [item.content for item in active_memories(db, user.id)] == ["Use weekly output"]


def _chat(db, email: str):
    user = User(email=email, password_hash="unused")
    db.add(user); db.flush()
    chat = Chat(user_id=user.id, title="history")
    db.add(chat); db.flush()
    return chat


def test_tool_results_never_appear_in_assistant_prose():
    """The contamination this change fixes: once results were flattened into the
    assistant's content, the model imitated the format instead of calling tools."""
    with SessionLocal() as db:
        chat = _chat(db, "noprose@example.com")
        events = [
            {"name": "execute_sql", "input": {"target": "jaffle_shop", "sql": "SELECT 1"}, "output": {"query_id": "q1"}, "tool_call_id": "call_abc"},
            {"name": "display_chart", "input": {"query_id": "q1"}, "output": {"chart_id": "c1"}, "tool_call_id": "call_def"},
        ]
        message = Message(chat_id=chat.id, role="assistant", content="here is the answer", tool_events=events)
        db.add(message); db.flush(); save_parts(db, message, message.content, events); db.commit()

        history = load_model_history(db, chat.id)

        assert [item["role"] for item in history] == ["assistant", "tool", "tool"]
        assert history[0]["content"] == "here is the answer"
        assert "[tool " not in history[0]["content"]
        assert "query_id" not in history[0]["content"]
        # Ids come from the model's own call ids, so each result pairs with its call.
        assert [call["id"] for call in history[0]["tool_calls"]] == ["call_abc", "call_def"]
        assert [item["tool_call_id"] for item in history[1:]] == ["call_abc", "call_def"]
        assert json.loads(history[0]["tool_calls"][0]["function"]["arguments"]) == {"target": "jaffle_shop", "sql": "SELECT 1"}
        assert json.loads(history[1]["content"]) == {"query_id": "q1"}


def test_save_parts_persists_the_models_own_tool_call_id():
    with SessionLocal() as db:
        chat = _chat(db, "callid@example.com")
        message = Message(chat_id=chat.id, role="assistant", content="x")
        db.add(message); db.flush()
        save_parts(db, message, "x", [{"name": "list_reports", "input": {}, "output": {}, "tool_call_id": "call_xyz"}])
        db.commit()

        part = db.query(MessagePart).filter(MessagePart.message_id == message.id, MessagePart.part_type == "tool").one()
        assert part.tool_call_id == "call_xyz"


def test_rows_written_before_tool_call_id_existed_still_pair_up():
    """Legacy parts carry no call id; a synthesised one must still be unique and matched."""
    with SessionLocal() as db:
        chat = _chat(db, "legacy@example.com")
        message = Message(chat_id=chat.id, role="assistant", content="old answer")
        db.add(message); db.flush()
        save_parts(db, message, "old answer", [{"name": "execute_sql", "input": {}, "output": {"query_id": "old"}}])
        db.commit()

        history = load_model_history(db, chat.id)
        ids = [call["id"] for call in history[0]["tool_calls"]]

        assert ids[0].startswith("call_")
        assert history[1]["tool_call_id"] == ids[0]


def test_messages_predating_structured_parts_are_replayed_from_tool_events():
    with SessionLocal() as db:
        chat = _chat(db, "noparts@example.com")
        message = Message(chat_id=chat.id, role="assistant", content="ancient answer", tool_events=[{"name": "inspect_file", "input": {"file_id": "f1"}, "output": {"sheets": []}}])
        db.add(message); db.commit()  # no save_parts at all

        history = load_model_history(db, chat.id)

        assert history[0]["content"] == "ancient answer"
        assert history[0]["tool_calls"][0]["function"]["name"] == "inspect_file"
        assert history[1]["role"] == "tool"


def test_a_failed_tool_is_replayed_as_a_failure_not_as_a_result():
    with SessionLocal() as db:
        chat = _chat(db, "toolfail@example.com")
        message = Message(chat_id=chat.id, role="assistant", content="")
        db.add(message); db.flush()
        save_parts(db, message, "", [{"name": "execute_sql", "input": {}, "output": {}, "tool_call_id": "call_bad"}])
        db.flush()
        part = db.query(MessagePart).filter(MessagePart.message_id == message.id, MessagePart.part_type == "tool").one()
        part.state = "error"; part.error_text = "no such column"
        db.commit()

        history = load_model_history(db, chat.id)

        assert history[1]["content"] == "Tool failed: no such column"


def test_a_user_message_stays_a_plain_user_message():
    with SessionLocal() as db:
        chat = _chat(db, "plain@example.com")
        message = Message(chat_id=chat.id, role="user", content="how many orders?")
        db.add(message); db.flush(); save_parts(db, message, message.content); db.commit()

        assert load_model_history(db, chat.id) == [{"role": "user", "content": "how many orders?"}]


def test_oversized_tool_output_is_capped_in_history():
    with SessionLocal() as db:
        chat = _chat(db, "big@example.com")
        huge = {"rows": [{"v": "x" * 100} for _ in range(200)]}
        message = Message(chat_id=chat.id, role="assistant", content="big")
        db.add(message); db.flush()
        save_parts(db, message, "big", [{"name": "execute_sql", "input": {}, "output": huge, "tool_call_id": "call_big"}])
        db.commit()

        history = load_model_history(db, chat.id)

        assert len(history[1]["content"]) <= HISTORY_TOOL_OUTPUT_CHARS + 32
        assert history[1]["content"].endswith("(truncated in history)")
