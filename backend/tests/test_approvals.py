from dataclasses import replace

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import app
from app.models import Chat, PendingApproval, User
from app.services.approvals import decide, granted_fingerprints, record_request
from app.services.tools import ToolContext, registry
from app.services.tools.errors import ErrorCode
from app.services.tools.policy import Budget, ToolPolicy, invocation_fingerprint


def setup_function():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def _context(approvals=frozenset()) -> ToolContext:
    policy = replace(ToolPolicy.from_settings(), approval_required=frozenset({"export_result"}))
    return ToolContext(
        db=None,
        user=User(id="user-1"),
        chat=Chat(id="chat-1"),
        budget=Budget(policy=policy, approvals=approvals),
    )


def test_a_tool_needing_approval_is_held_and_names_its_fingerprint():
    args = {"query_id": "q1"}
    context = _context()

    result = registry.dispatch("export_result", args, context)

    assert result["code"] == str(ErrorCode.POLICY_VIOLATION)
    assert result["approval_required"]["tool"] == "export_result"
    assert result["approval_required"]["fingerprint"] == invocation_fingerprint("export_result", args)
    decision = next(item for item in context.budget.trail if item.action == "approval_required")
    assert decision.detail["args"] == args


def test_a_granted_fingerprint_lets_that_exact_call_through():
    args = {"query_id": "missing-query"}
    granted = frozenset({invocation_fingerprint("export_result", args)})
    with SessionLocal() as db:
        user = User(id="u1", email="grant@example.com", password_hash="x")
        chat = Chat(id="c1", user_id="u1")
        db.add_all([user, chat])
        db.commit()
        context = replace(_context(approvals=granted), db=db, user=user, chat=chat)

        result = registry.dispatch("export_result", args, context)

    # Reaching the handler is the proof: it now fails on the missing query, not on policy.
    assert result["code"] == str(ErrorCode.RESOURCE_UNAVAILABLE)


def test_a_grant_does_not_cover_different_arguments():
    granted = frozenset({invocation_fingerprint("export_result", {"query_id": "q1"})})

    result = registry.dispatch("export_result", {"query_id": "q2"}, _context(approvals=granted))

    assert result["code"] == str(ErrorCode.POLICY_VIOLATION)


def test_requests_are_idempotent_per_fingerprint_and_grants_are_scoped_to_the_chat():
    with SessionLocal() as db:
        user = User(id="u1", email="a@example.com", password_hash="x")
        chat = Chat(id="c1", user_id="u1")
        other = Chat(id="c2", user_id="u1")
        db.add_all([user, chat, other])
        db.commit()

        first = record_request(db, user, "c1", None, "export_result", "fp-1", {"query_id": "q1"})
        again = record_request(db, user, "c1", None, "export_result", "fp-1", {"query_id": "q1"})
        assert first.id == again.id
        assert db.query(PendingApproval).count() == 1

        assert granted_fingerprints(db, "u1", "c1") == frozenset()
        decide(db, user, first.id, "approved")
        assert granted_fingerprints(db, "u1", "c1") == frozenset({"fp-1"})
        assert granted_fingerprints(db, "u1", "c2") == frozenset()


def test_approval_endpoints_are_scoped_to_the_owner():
    with TestClient(app) as owner, TestClient(app) as stranger:
        assert owner.post("/auth/register", json={"email": "owner@example.com", "password": "a-long-test-password"}).status_code == 200
        assert stranger.post("/auth/register", json={"email": "other@example.com", "password": "a-long-test-password"}).status_code == 200
        chat_id = owner.post("/chats", json={"title": "approvals"}).json()["id"]

        with SessionLocal() as db:
            user = db.query(User).filter(User.email == "owner@example.com").one()
            record = record_request(db, user, chat_id, None, "export_result", "fp-http", {"query_id": "q1"})
            approval_id = record.id

        assert len(owner.get("/approvals").json()) == 1
        assert stranger.get("/approvals").json() == []
        assert stranger.post(f"/approvals/{approval_id}", json={"decision": "approved"}).status_code == 404

        approved = owner.post(f"/approvals/{approval_id}", json={"decision": "approved"})
        assert approved.status_code == 200
        assert approved.json()["decision"] == "approved"
        assert owner.get("/approvals").json() == []
        assert len(owner.get("/approvals", params={"pending": False}).json()) == 1


def test_a_decided_request_cannot_be_decided_again_and_rejects_bad_decisions():
    with TestClient(app) as client:
        assert client.post("/auth/register", json={"email": "twice@example.com", "password": "a-long-test-password"}).status_code == 200
        chat_id = client.post("/chats", json={"title": "approvals"}).json()["id"]
        with SessionLocal() as db:
            user = db.query(User).filter(User.email == "twice@example.com").one()
            approval_id = record_request(db, user, chat_id, None, "export_result", "fp-twice", {}).id

        assert client.post(f"/approvals/{approval_id}", json={"decision": "sideways"}).status_code == 400
        assert client.post(f"/approvals/{approval_id}", json={"decision": "rejected"}).status_code == 200
        assert client.post(f"/approvals/{approval_id}", json={"decision": "approved"}).status_code == 409
