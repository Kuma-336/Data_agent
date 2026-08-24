import json
from pathlib import Path
from fastapi import Depends, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session
from .config import settings
from .db import Base, engine, get_db
from .migrations import upgrade_schema
from .dependencies import current_user
from .errors import ApiError, normalize_error
from .models import Chat, DataSource, Memory, Message, MessagePart, QueryResult, UploadedFile, User
from .schemas import ChatCreate, ChatMessageInput, DataSourceInput, DataSourceOut, LoginInput, RegisterInput, UserOut
from .security import create_access_token, decrypt_secret, encrypt_secret, hash_password, verify_password
from .services.agent import run_agent
from .services.storage import save_upload
from .worker import compact_chat, extract_memory
from .services.conversation import save_parts

app = FastAPI(title="Personal Data Agent", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


@app.on_event("startup")
def startup() -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(bind=engine)
    upgrade_schema()


@app.exception_handler(ApiError)
async def api_error_handler(_: Request, error: ApiError) -> Response:
    return Response(content=json.dumps(error.as_payload()), status_code=error.status_code, media_type="application/json")


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_: Request, error: RequestValidationError) -> Response:
    payload = ApiError("VALIDATION_FAILED", "The submitted data is invalid.", "Correct the highlighted fields and retry.", 422).as_payload()
    payload["details"] = error.errors()
    return Response(content=json.dumps(payload, default=str), status_code=422, media_type="application/json")


@app.get("/health")
def health() -> dict:
    return {"status": "alive"}


@app.get("/ready")
def ready(db: Session = Depends(get_db)) -> dict:
    try:
        db.execute(text("SELECT 1"))
    except Exception as exc:
        raise ApiError("DATABASE_UNAVAILABLE", "The application database is not ready.", "Wait for PostgreSQL or inspect `docker compose logs db`.", 503) from exc
    return {"status": "ready", "model": settings.llm_model}


@app.post("/auth/register", response_model=UserOut)
def register(payload: RegisterInput, response: Response, db: Session = Depends(get_db)) -> User:
    if db.query(User).filter(User.email == payload.email.lower()).first():
        raise HTTPException(409, "Email is already registered")
    user = User(email=payload.email.lower(), password_hash=hash_password(payload.password))
    db.add(user)
    db.commit()
    db.refresh(user)
    response.set_cookie("access_token", create_access_token(user.id), httponly=True, samesite="lax", max_age=1800)
    return user


@app.post("/auth/login", response_model=UserOut)
def login(payload: LoginInput, response: Response, db: Session = Depends(get_db)) -> User:
    user = db.query(User).filter(User.email == payload.email.lower()).first()
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(401, "Invalid email or password")
    response.set_cookie("access_token", create_access_token(user.id), httponly=True, samesite="lax", max_age=1800)
    return user


@app.post("/auth/logout", status_code=204)
def logout(response: Response) -> Response:
    response.delete_cookie("access_token")
    return response


@app.get("/auth/me", response_model=UserOut)
def me(user: User = Depends(current_user)) -> User:
    return user


@app.post("/files")
def upload_file(upload: UploadFile = File(...), user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    record = save_upload(db, user, upload)
    return {"id": record.id, "name": record.original_name, "size_bytes": record.size_bytes, "schema": record.file_schema}


@app.get("/files")
def list_files(user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    return [{"id": item.id, "name": item.original_name, "schema": item.file_schema} for item in db.query(UploadedFile).filter(UploadedFile.user_id == user.id).all()]


@app.post("/connections", response_model=DataSourceOut)
def create_connection(payload: DataSourceInput, user: User = Depends(current_user), db: Session = Depends(get_db)) -> DataSource:
    if payload.host.lower() not in settings.allowed_hosts:
        raise HTTPException(403, "Database host is not allowlisted")
    source = DataSource(user_id=user.id, name=payload.name, host=payload.host, port=payload.port, database_name=payload.database_name, username=payload.username, encrypted_password=encrypt_secret(payload.password), sslmode=payload.sslmode)
    db.add(source)
    db.commit()
    db.refresh(source)
    return source


@app.get("/connections", response_model=list[DataSourceOut])
def list_connections(user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[DataSource]:
    return db.query(DataSource).filter(DataSource.user_id == user.id).all()


@app.post("/connections/{connection_id}/test")
def test_connection(connection_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    source = _source_for_user(db, user.id, connection_id)
    url = URL.create("postgresql+psycopg", username=source.username, password=decrypt_secret(source.encrypted_password), host=source.host, port=source.port, database=source.database_name, query={"sslmode": source.sslmode})
    engine_ = create_engine(url, connect_args={"options": "-c statement_timeout=30000"})
    try:
        with engine_.connect() as conn:
            version = conn.execute(text("SELECT version()" )).scalar_one()
            rows = conn.execute(text("SELECT table_schema, table_name FROM information_schema.tables WHERE table_schema NOT IN ('pg_catalog', 'information_schema') ORDER BY 1, 2 LIMIT 200")).mappings().all()
        source.schema_cache = {"tables": [dict(row) for row in rows]}
        db.commit()
        return {"ok": True, "version": version, "schema": source.schema_cache}
    except Exception as exc:
        raise HTTPException(400, f"Connection test failed: {exc}") from exc
    finally:
        engine_.dispose()


@app.post("/chats")
def create_chat(payload: ChatCreate, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    chat = Chat(user_id=user.id, title=payload.title)
    db.add(chat)
    db.commit()
    return {"id": chat.id, "title": chat.title}


@app.get("/chats")
def list_chats(limit: int = 30, offset: int = 0, user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    chats = db.query(Chat).filter(Chat.user_id == user.id, Chat.deleted_at.is_(None)).order_by(Chat.updated_at.desc()).offset(offset).limit(min(limit, 100)).all()
    return [{"id": chat.id, "title": chat.title, "updated_at": chat.updated_at} for chat in chats]


@app.get("/chats/{chat_id}")
def get_chat(chat_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    chat = _chat_for_user(db, user.id, chat_id)
    messages = db.query(Message).filter(Message.chat_id == chat.id, Message.superseded_at.is_(None)).order_by(Message.created_at).all()
    return {"id": chat.id, "title": chat.title, "messages": [{"id": m.id, "role": m.role, "content": m.content, "tool_events": m.tool_events, "parts": [{"type": p.part_type, "state": p.state, "tool_name": p.tool_name, "input": p.input, "output": p.output, "error": p.error_text, "text": p.text} for p in db.query(MessagePart).filter(MessagePart.message_id == m.id).order_by(MessagePart.part_order).all()]} for m in messages]}


@app.patch("/chats/{chat_id}")
def rename_chat(chat_id: str, payload: ChatCreate, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    chat = _chat_for_user(db, user.id, chat_id)
    chat.title = payload.title
    db.commit()
    return {"id": chat.id, "title": chat.title}


@app.delete("/chats/{chat_id}", status_code=204)
def delete_chat(chat_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> Response:
    chat = _chat_for_user(db, user.id, chat_id)
    from datetime import datetime, timezone
    chat.deleted_at = datetime.now(timezone.utc)
    db.commit()
    return Response(status_code=204)


@app.post("/chats/{chat_id}/restore")
def restore_chat(chat_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    chat = db.query(Chat).filter(Chat.id == chat_id, Chat.user_id == user.id).first()
    if not chat:
        raise HTTPException(404, "Chat not found")
    chat.deleted_at = None
    db.commit()
    return {"id": chat.id, "title": chat.title}


@app.post("/chats/{chat_id}/messages")
def send_message(chat_id: str, payload: ChatMessageInput, user: User = Depends(current_user), db: Session = Depends(get_db)) -> StreamingResponse:
    chat = _chat_for_user(db, user.id, chat_id)
    user_message = Message(chat_id=chat.id, role="user", content=payload.content)
    db.add(user_message)
    db.flush()
    save_parts(db, user_message, payload.content)
    from datetime import datetime, timezone
    chat.updated_at = datetime.now(timezone.utc)
    db.commit()

    def stream():
        emitted: list[tuple[str, dict]] = []
        def emit(kind: str, data: dict) -> None:
            emitted.append((kind, data))
        try:
            answer, events = run_agent(db, user, chat, payload.content, payload.file_ids, payload.connection_ids, emit)
            for kind, data in emitted:
                yield _sse(kind, data)
            assistant = Message(chat_id=chat.id, role="assistant", content=answer, tool_events=events)
            db.add(assistant)
            db.flush()
            save_parts(db, assistant, answer, events)
            chat.updated_at = datetime.now(timezone.utc)
            db.commit()
            yield _sse("answer", {"content": answer, "tool_events": events})
        except Exception as exc:
            yield _sse("error", normalize_error(exc).as_payload())
        finally:
            try:
                extract_memory.send(user.id, chat.id, user_message.id)
                compact_chat.send(chat.id)
            except Exception:
                # A temporary Redis failure must not fail a successfully completed answer.
                pass
    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@app.get("/queries/{query_id}")
def get_query(query_id: str, offset: int = 0, limit: int = 100, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    result = db.query(QueryResult).filter(QueryResult.id == query_id, QueryResult.user_id == user.id).first()
    if not result:
        raise HTTPException(404, "Query result not found")
    return {"id": result.id, "columns": result.columns, "rows": result.rows[offset:offset + min(limit, settings.max_query_rows)], "sql": result.sql, "source": result.source}


@app.get("/memories")
def list_memories(user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    records = db.query(Memory).filter(Memory.user_id == user.id, Memory.superseded.is_(False), Memory.superseded_by.is_(None)).order_by(Memory.updated_at.desc(), Memory.created_at.desc()).all()
    return [{"id": item.id, "category": item.category, "content": item.content, "source_chat_id": item.source_chat_id, "updated_at": item.updated_at or item.created_at} for item in records]


@app.patch("/memories/{memory_id}")
def update_memory(memory_id: str, payload: dict, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    memory = db.query(Memory).filter(Memory.id == memory_id, Memory.user_id == user.id).first()
    content = " ".join(str(payload.get("content", "")).split())
    if not memory or not content:
        raise HTTPException(404 if not memory else 422, "Memory not found or empty")
    memory.content = content[:1000]
    db.commit()
    return {"id": memory.id, "content": memory.content}


@app.delete("/memories/{memory_id}", status_code=204)
def delete_memory(memory_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> Response:
    memory = db.query(Memory).filter(Memory.id == memory_id, Memory.user_id == user.id).first()
    if not memory:
        raise HTTPException(404, "Memory not found")
    db.delete(memory)
    db.commit()
    return Response(status_code=204)


@app.patch("/settings/memory")
def set_memory_enabled(payload: dict, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    user.memory_enabled = bool(payload.get("enabled", True))
    db.commit()
    return {"enabled": user.memory_enabled}


def _chat_for_user(db: Session, user_id: str, chat_id: str) -> Chat:
    chat = db.query(Chat).filter(Chat.id == chat_id, Chat.user_id == user_id).first()
    if not chat:
        raise HTTPException(404, "Chat not found")
    return chat


def _source_for_user(db: Session, user_id: str, source_id: str) -> DataSource:
    source = db.query(DataSource).filter(DataSource.id == source_id, DataSource.user_id == user_id).first()
    if not source:
        raise HTTPException(404, "Data source not found")
    return source


def _sse(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"
