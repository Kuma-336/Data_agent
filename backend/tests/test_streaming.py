"""The agent loop must hand each tool event to its caller as it happens.

Before the generator conversion the SSE route buffered every event into a list
and only yielded after the whole turn finished, so the browser waited out all
model round-trips before seeing anything.
"""

from types import SimpleNamespace

import pytest

from app.db import Base, SessionLocal, engine
from app.models import Chat, User
from app.services import agent as agent_module
from app.services.agent import iter_agent, run_agent


def setup_function():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


class _FakeCompletions:
    """Replays scripted assistant messages and records when each was requested."""

    def __init__(self, script, observed):
        self._script = list(script)
        self._observed = observed

    def create(self, **kwargs):
        self._observed.append("model_call")
        message = self._script.pop(0)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1))


def _tool_call(call_id: str, name: str, arguments: str):
    return SimpleNamespace(id=call_id, function=SimpleNamespace(name=name, arguments=arguments))


def _assistant(content=None, tool_calls=None):
    return SimpleNamespace(content=content, tool_calls=tool_calls, model_dump=lambda exclude_none=True: {"role": "assistant", "content": content or ""})


@pytest.fixture
def scripted(monkeypatch):
    """Two turns of tool calls, then a final answer, with a call-order log."""
    observed: list[str] = []
    script = [
        _assistant(tool_calls=[_tool_call("c1", "list_context", "{}")]),
        _assistant(tool_calls=[_tool_call("c2", "list_reports", "{}")]),
        _assistant(content="done"),
    ]
    monkeypatch.setattr(agent_module, "OpenAI", lambda **kwargs: SimpleNamespace(chat=SimpleNamespace(completions=_FakeCompletions(script, observed))))
    return observed


def _user_and_chat(db):
    user = User(email="stream@example.com", password_hash="x")
    db.add(user)
    db.commit()
    db.refresh(user)
    chat = Chat(user_id=user.id, title="stream")
    db.add(chat)
    db.commit()
    db.refresh(chat)
    return user, chat


def test_each_tool_event_is_yielded_before_the_next_model_call(scripted):
    """The ordering proves streaming: a buffered implementation emits every
    model_call before any tool event."""
    with SessionLocal() as db:
        user, chat = _user_and_chat(db)
        for kind, data in iter_agent(db, user, chat, "hello", [], []):
            scripted.append(f"{kind}:{data.get('name', '')}" if kind == "tool" else kind)

    assert scripted == [
        "model_call",
        "tool:list_context",
        "model_call",
        "tool:list_reports",
        "model_call",
        "answer",
    ]


def test_the_terminal_event_is_always_an_answer(scripted):
    with SessionLocal() as db:
        user, chat = _user_and_chat(db)
        kinds = [kind for kind, _ in iter_agent(db, user, chat, "hello", [], [])]

    assert kinds[-1] == "answer"
    assert kinds.count("answer") == 1


def test_run_agent_still_returns_the_drained_result(scripted):
    """The three non-streaming callers keep the old contract."""
    with SessionLocal() as db:
        user, chat = _user_and_chat(db)
        answer, events = run_agent(db, user, chat, "hello", [], [])

    assert answer == "done"
    assert [event["name"] for event in events] == ["list_context", "list_reports"]
