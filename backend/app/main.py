import json
from pathlib import Path
from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, Response, UploadFile
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
from .models import ChartArtifact, Chat, DataProfile, DataSource, ExportArtifact, Memory, Message, MessagePart, ProjectContextDocument, QueryResult, Report, ReportVersion, UploadedFile, User
from .schemas import ChatCreate, ChatMessageInput, DataSourceInput, DataSourceOut, LoginInput, RegisterInput, UserOut
from .security import create_access_token, create_mcp_token, decode_mcp_token, decrypt_secret, encrypt_secret, hash_password, verify_password
from .services.agent import run_agent
from .services.storage import save_upload
from .worker import compact_chat, extract_memory
from .services.conversation import save_parts
from .services.analytics import create_chart, export_csv, profile_file
from .services.reports import export_report, save_report, serialize_report
from .services.mcp import call_tool as mcp_call_tool, jsonrpc_error, jsonrpc_result, tool_definitions as mcp_tool_definitions

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


@app.post("/mcp/tokens")
def create_mcp_access_token(user: User = Depends(current_user)) -> dict:
    """Return a bearer token once for configuring an external MCP client."""
    if not settings.mcp_enabled:
        raise HTTPException(404, "MCP endpoint is disabled")
    return {"token": create_mcp_token(user.id), "expires_in_days": settings.mcp_token_ttl_days, "mcp_url": "/mcp"}


@app.post("/mcp")
async def mcp(request: Request, authorization: str | None = Header(default=None), db: Session = Depends(get_db)) -> Response:
    """Streamable HTTP MCP endpoint; disabled unless MCP_ENABLED=true."""
    if not settings.mcp_enabled:
        raise HTTPException(404, "MCP endpoint is disabled")
    if not authorization or not authorization.startswith("Bearer "):
        return Response(status_code=401, headers={"WWW-Authenticate": "Bearer"})
    try:
        user_id = decode_mcp_token(authorization[7:])
        user = db.get(User, user_id)
        if not user:
            raise ValueError("Unknown user")
        payload = await request.json()
    except Exception:
        return Response(status_code=401, headers={"WWW-Authenticate": "Bearer"})
    request_id, method = payload.get("id"), payload.get("method")
    if request_id is None:
        return Response(status_code=202)
    if method == "initialize":
        result, headers = jsonrpc_result(request_id, {"protocolVersion": payload.get("params", {}).get("protocolVersion", "2025-03-26"), "capabilities": {"tools": {"listChanged": False}}, "serverInfo": {"name": "personal-data-agent", "version": app.version}}, str(__import__("uuid").uuid4()))
        return Response(content=json.dumps(result, default=str), media_type="application/json", headers=headers)
    if method == "tools/list":
        result, headers = jsonrpc_result(request_id, {"tools": mcp_tool_definitions()})
        return Response(content=json.dumps(result), media_type="application/json", headers=headers)
    if method == "tools/call":
        params = payload.get("params", {})
        try:
            output = mcp_call_tool(db, user, str(params.get("name", "")), dict(params.get("arguments") or {}))
            result, headers = jsonrpc_result(request_id, {"content": [{"type": "text", "text": json.dumps(output, ensure_ascii=False, default=str)}]})
        except Exception as exc:
            result, headers = jsonrpc_result(request_id, {"content": [{"type": "text", "text": json.dumps({"error": str(exc)}, ensure_ascii=False)}], "isError": True})
        return Response(content=json.dumps(result, default=str), media_type="application/json", headers=headers)
    return Response(content=json.dumps(jsonrpc_error(request_id, -32601, f"Method not found: {method}")), media_type="application/json")


@app.post("/files")
def upload_file(upload: UploadFile = File(...), user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    record = save_upload(db, user, upload)
    return {"id": record.id, "name": record.original_name, "size_bytes": record.size_bytes, "schema": record.file_schema}


@app.get("/files")
def list_files(user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    return [{"id": item.id, "name": item.original_name, "schema": item.file_schema} for item in db.query(UploadedFile).filter(UploadedFile.user_id == user.id).all()]


@app.post("/files/{file_id}/profile")
def profile_uploaded_file(file_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    file = db.query(UploadedFile).filter(UploadedFile.id == file_id, UploadedFile.user_id == user.id).first()
    if not file:
        raise HTTPException(404, "File not found")
    record = profile_file(db, user, file)
    return {"id": record.id, "profile": record.profile}


@app.get("/assets/profiles")
def list_profiles(user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    return [{"id": item.id, "asset_kind": item.asset_kind, "asset_id": item.asset_id, "profile": item.profile} for item in db.query(DataProfile).filter(DataProfile.user_id == user.id).order_by(DataProfile.created_at.desc()).limit(50).all()]


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


@app.post("/charts")
def create_chart_endpoint(payload: dict, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    chart = create_chart(db, user, str(payload.get("chat_id", "")), str(payload.get("query_id", "")), payload)
    return {"id": chart.id, "spec": chart.spec}


@app.get("/charts/{chart_id}")
def get_chart(chart_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    chart = db.query(ChartArtifact).filter(ChartArtifact.id == chart_id, ChartArtifact.user_id == user.id).first()
    if not chart:
        raise HTTPException(404, "Chart not found")
    result = db.query(QueryResult).filter(QueryResult.id == chart.query_id, QueryResult.user_id == user.id).first()
    return {"id": chart.id, "spec": chart.spec, "rows": result.rows if result else [], "columns": result.columns if result else []}


@app.post("/queries/{query_id}/export")
def export_query(query_id: str, payload: dict, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    if payload.get("format", "csv") != "csv":
        raise HTTPException(422, "Only CSV export is currently supported")
    artifact = export_csv(db, user, str(payload.get("chat_id", "")), query_id)
    return {"id": artifact.id, "format": artifact.format, "download_url": f"/exports/{artifact.id}"}


@app.get("/exports/{export_id}")
def download_export(export_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    from fastapi.responses import FileResponse
    artifact = db.query(ExportArtifact).filter(ExportArtifact.id == export_id, ExportArtifact.user_id == user.id).first()
    if not artifact:
        raise HTTPException(404, "Export not found")
    return FileResponse(artifact.storage_path, filename=f"query-{artifact.query_id}.csv", media_type="text/csv")


@app.get("/context-documents")
def list_context_documents(user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    return [{"id": item.id, "name": item.name, "path": item.path, "content": item.content, "updated_at": item.updated_at} for item in db.query(ProjectContextDocument).filter(ProjectContextDocument.user_id == user.id).all()]


@app.put("/context-documents/{name}")
def upsert_context_document(name: str, payload: dict, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    content = str(payload.get("content", ""))[:50_000]
    document = db.query(ProjectContextDocument).filter(ProjectContextDocument.user_id == user.id, ProjectContextDocument.name == name).first()
    if not document:
        document = ProjectContextDocument(user_id=user.id, name=name[:120], path=f"/context/{name[:120]}", content=content)
        db.add(document)
    else:
        document.content = content
    db.commit(); db.refresh(document)
    return {"id": document.id, "name": document.name, "content": document.content}


@app.get("/reports")
def list_reports(user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    return [{"id": item.id, "title": item.title, "chat_id": item.chat_id, "updated_at": item.updated_at} for item in db.query(Report).filter(Report.user_id == user.id, Report.archived_at.is_(None)).order_by(Report.updated_at.desc()).all()]


@app.post("/reports")
def create_report(payload: dict, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    report = save_report(db, user, str(payload.get("title", "Untitled report")), payload.get("blocks", []), payload.get("chat_id"))
    return serialize_report(db, user, report)


@app.get("/reports/{report_id}")
def get_report(report_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    report = db.query(Report).filter(Report.id == report_id, Report.user_id == user.id).first()
    if not report: raise HTTPException(404, "Report not found")
    return serialize_report(db, user, report)


@app.patch("/reports/{report_id}")
def update_report(report_id: str, payload: dict, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    report = db.query(Report).filter(Report.id == report_id, Report.user_id == user.id).first()
    if not report: raise HTTPException(404, "Report not found")
    report = save_report(db, user, str(payload.get("title", report.title)), payload.get("blocks", serialize_report(db, user, report)["blocks"]), report.chat_id, report)
    return serialize_report(db, user, report)


@app.delete("/reports/{report_id}", status_code=204)
def delete_report(report_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> Response:
    report = db.query(Report).filter(Report.id == report_id, Report.user_id == user.id).first()
    if not report: raise HTTPException(404, "Report not found")
    db.delete(report); db.commit(); return Response(status_code=204)


@app.get("/reports/{report_id}/versions")
def list_report_versions(report_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    report = db.query(Report).filter(Report.id == report_id, Report.user_id == user.id).first()
    if not report: raise HTTPException(404, "Report not found")
    return [{"id": item.id, "version": item.version, "created_at": item.created_at} for item in db.query(ReportVersion).filter(ReportVersion.report_id == report.id).order_by(ReportVersion.version.desc()).all()]


@app.post("/reports/{report_id}/restore/{version_id}")
def restore_report(report_id: str, version_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    report = db.query(Report).filter(Report.id == report_id, Report.user_id == user.id).first(); version = db.query(ReportVersion).filter(ReportVersion.id == version_id, ReportVersion.report_id == report_id).first()
    if not report or not version: raise HTTPException(404, "Report version not found")
    report = save_report(db, user, version.title, version.blocks, report.chat_id, report); return serialize_report(db, user, report)


@app.get("/reports/{report_id}/export")
def download_report(report_id: str, format: str = "html", user: User = Depends(current_user), db: Session = Depends(get_db)):
    from fastapi.responses import FileResponse
    report = db.query(Report).filter(Report.id == report_id, Report.user_id == user.id).first()
    if not report: raise HTTPException(404, "Report not found")
    path = export_report(db, user, report, format); return FileResponse(path, filename=f"{report.title}.{format}")


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
