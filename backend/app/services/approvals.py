"""Approval records for high-risk tool calls.

A turn that needs approval ends rather than suspending: the hook refuses the
call and records it here, and a later turn carries the granted fingerprints in
its budget. Grants are per fingerprint, so changing the arguments needs a new
approval.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..models import AgentRun, PendingApproval, User

DECISIONS = ("approved", "rejected")


def granted_fingerprints(db: Session, user_id: str, chat_id: str) -> frozenset[str]:
    records = (
        db.query(PendingApproval)
        .filter(
            PendingApproval.user_id == user_id,
            PendingApproval.chat_id == chat_id,
            PendingApproval.decision == "approved",
        )
        .all()
    )
    return frozenset(record.fingerprint for record in records)


def record_request(db: Session, user: User, chat_id: str, run: AgentRun | None, tool_name: str, fingerprint: str, args: dict) -> PendingApproval:
    """Idempotent per fingerprint: a repeated request reuses the open record."""
    existing = (
        db.query(PendingApproval)
        .filter(
            PendingApproval.user_id == user.id,
            PendingApproval.chat_id == chat_id,
            PendingApproval.fingerprint == fingerprint,
            PendingApproval.decision.is_(None),
        )
        .first()
    )
    if existing:
        return existing
    record = PendingApproval(
        user_id=user.id,
        chat_id=chat_id,
        run_id=run.id if run else None,
        tool_name=tool_name,
        fingerprint=fingerprint,
        args=args,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


def decide(db: Session, user: User, approval_id: str, decision: str) -> PendingApproval:
    if decision not in DECISIONS:
        raise HTTPException(400, f"decision must be one of {', '.join(DECISIONS)}")
    record = (
        db.query(PendingApproval)
        .filter(PendingApproval.id == approval_id, PendingApproval.user_id == user.id)
        .first()
    )
    if not record:
        raise HTTPException(404, "Approval request not found")
    if record.decision:
        raise HTTPException(409, f"This request was already {record.decision}")
    record.decision = decision
    record.decided_at = datetime.now(UTC)
    db.commit()
    db.refresh(record)
    return record


def serialize(record: PendingApproval) -> dict:
    return {
        "id": record.id,
        "chat_id": record.chat_id,
        "tool": record.tool_name,
        "args": record.args,
        "fingerprint": record.fingerprint,
        "decision": record.decision,
        "created_at": record.created_at,
        "decided_at": record.decided_at,
    }
