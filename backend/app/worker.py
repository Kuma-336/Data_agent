import dramatiq
from dramatiq.brokers.redis import RedisBroker
from .config import settings
from .db import SessionLocal
from .models import Chat, EvalRun
from .services.evals import run_suite
from .services.memory_manager import extract_memories, maybe_compact
from .services.tools import registry

dramatiq.set_broker(RedisBroker(url=settings.redis_url))


@dramatiq.actor
def extract_memory(user_id: str, chat_id: str, source_message_id: str | None = None) -> None:
    with SessionLocal() as db:
        extract_memories(db, user_id, chat_id, source_message_id)


@dramatiq.actor
def compact_chat(chat_id: str) -> None:
    with SessionLocal() as db:
        maybe_compact(db, chat_id, registry.definitions())


@dramatiq.actor
def run_eval(eval_run_id: str) -> None:
    with SessionLocal() as db:
        # Dramatiq provides at-least-once delivery. Atomically claim only queued
        # runs so a redelivered message cannot execute a billable suite twice.
        claimed = db.query(EvalRun).filter(
            EvalRun.id == eval_run_id,
            EvalRun.status == "queued",
        ).update({"status": "running"}, synchronize_session=False)
        db.commit()
        if claimed != 1:
            return
        run = db.get(EvalRun, eval_run_id)
        if not run:
            return
        try:
            run_suite(db, run)
        except Exception as exc:
            run.status = "failed"; run.error = str(exc); db.commit()
