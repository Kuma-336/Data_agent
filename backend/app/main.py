import json
from datetime import UTC, datetime
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
from .models import AgentRun, ChartArtifact, Chat, DataProfile, DataSource, EvalCase, EvalCaseResult, EvalRun, EvalSuite, ExportArtifact, Memory, Message, MessagePart, ProjectContextDocument, QueryResult, Report, ReportVersion, UploadedFile, User
from .schemas import ChatCreate, ChatMessageInput, DataSourceInput, DataSourceOut, LoginInput, RegisterInput, UserOut
from .security import create_access_token, create_mcp_token, decode_mcp_token, decrypt_secret, encrypt_secret, hash_password, verify_password
from .services.agent import run_agent
from .services.storage import save_upload
from .worker import compact_chat, extract_memory, run_eval
from .services.conversation import save_parts
from .services.analytics import create_chart, export_csv, profile_file
from .services.reports import export_report, save_report, serialize_report
from .services.mcp import call_tool as mcp_call_tool, jsonrpc_error, jsonrpc_result, tool_definitions as mcp_tool_definitions
from .services.runs import serialize_run, start_run, verify_run
from .services.jaffle_shop import list_tables as list_jaffle_shop_tables
from .services.eval_fixtures import import_fixture
from .services import context_store
from .services.context_store import ContextTarget
from .services.context_sync import sync_connection_context, sync_file_context, sync_sample_context

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
    try:
        sync_file_context(record)
    except Exception:
        # Context documents are a read-time optimisation; never fail the upload for them.
        pass
    return {"id": record.id, "name": record.original_name, "size_bytes": record.size_bytes, "schema": record.file_schema}


@app.get("/files")
def list_files(user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    return [{"id": item.id, "name": item.original_name, "schema": item.file_schema} for item in db.query(UploadedFile).filter(UploadedFile.user_id == user.id).all()]


@app.get("/sample-tables/jaffle-shop")
def list_jaffle_shop_sample_tables(user: User = Depends(current_user)) -> list[dict]:
    """List read-only sample tables that can be attached like an uploaded file."""
    return list_jaffle_shop_tables()


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
    run = start_run(db, user.id, chat.id, user_message.id, payload.content, payload.file_ids, payload.connection_ids, payload.jaffle_shop_tables)

    def stream():
        emitted: list[tuple[str, dict]] = []
        def emit(kind: str, data: dict) -> None:
            emitted.append((kind, data))
        try:
            emitted.append(("tool", {"name": "execution_plan", "input": {}, "output": {"run_id": run.id, "steps": run.plan}}))
            answer, events = run_agent(db, user, chat, payload.content, payload.file_ids, payload.connection_ids, emit, run, payload.jaffle_shop_tables)
            verifier = verify_run(db, run, answer, events)
            for kind, data in emitted:
                yield _sse(kind, data)
            assistant = Message(chat_id=chat.id, role="assistant", content=answer, tool_events=events)
            db.add(assistant)
            db.flush()
            save_parts(db, assistant, answer, events)
            chat.updated_at = datetime.now(timezone.utc)
            db.commit()
            yield _sse("tool", {"name": "verifier", "input": {}, "output": {"run_id": run.id, **verifier}})
            yield _sse("answer", {"content": answer, "tool_events": events, "run_id": run.id, "verifier": verifier})
        except Exception as exc:
            run.status = "failed"; db.commit()
            yield _sse("error", normalize_error(exc).as_payload())
        finally:
            try:
                extract_memory.send(user.id, chat.id, user_message.id)
                compact_chat.send(chat.id)
            except Exception:
                # A temporary Redis failure must not fail a successfully completed answer.
                pass
    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@app.get("/chats/{chat_id}/runs")
def list_agent_runs(chat_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    _chat_for_user(db, user.id, chat_id)
    return [{"id": run.id, "status": run.status, "prompt": run.prompt, "plan": run.plan, "verifier": run.verifier, "started_at": run.started_at, "completed_at": run.completed_at} for run in db.query(AgentRun).filter(AgentRun.user_id == user.id, AgentRun.chat_id == chat_id).order_by(AgentRun.started_at.desc()).all()]


@app.get("/runs/{run_id}")
def get_agent_run(run_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    run = db.query(AgentRun).filter(AgentRun.id == run_id, AgentRun.user_id == user.id).first()
    if not run: raise HTTPException(404, "Run not found")
    return serialize_run(db, run)


@app.get("/eval-suites")
def list_eval_suites(user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    return [{"id": item.id, "name": item.name, "description": item.description, "fixture_hash": item.fixture_hash, "dataset": item.dataset, "case_count": db.query(EvalCase).filter(EvalCase.suite_id == item.id).count()} for item in db.query(EvalSuite).filter(EvalSuite.user_id == user.id).all()]


@app.post("/eval-suites")
def create_eval_suite(payload: dict, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    name = str(payload.get("name", "")).strip()
    if not name: raise HTTPException(422, "Suite name is required")
    suite = EvalSuite(user_id=user.id, name=name[:160], description=str(payload.get("description", ""))[:4000]); db.add(suite); db.commit(); db.refresh(suite)
    return {"id": suite.id, "name": suite.name, "description": suite.description}


@app.post("/eval-suites/import")
def import_eval_suite(payload: dict, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    fixture = payload.get("fixture")
    target_id = str(payload.get("target_id", "")).strip()
    if not isinstance(fixture, dict) or not target_id:
        raise HTTPException(422, "fixture and target_id are required")
    target_type = fixture.get("suite", {}).get("target_type") if isinstance(fixture.get("suite"), dict) else None
    if target_type == "file":
        owned = db.query(UploadedFile).filter(UploadedFile.id == target_id, UploadedFile.user_id == user.id).first()
    elif target_type == "connection":
        owned = db.query(DataSource).filter(DataSource.id == target_id, DataSource.user_id == user.id).first()
    else:
        owned = None
    if not owned:
        raise HTTPException(404, "Evaluation resource not found")
    try:
        suite, imported = import_fixture(db, user, fixture, target_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"id": suite.id, "name": suite.name, "fixture_hash": suite.fixture_hash, "case_count": db.query(EvalCase).filter(EvalCase.suite_id == suite.id).count(), "imported": imported}


@app.get("/eval-suites/{suite_id}")
def get_eval_suite(suite_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    suite = db.query(EvalSuite).filter(EvalSuite.id == suite_id, EvalSuite.user_id == user.id).first()
    if not suite: raise HTTPException(404, "Evaluation suite not found")
    cases = db.query(EvalCase).filter(EvalCase.suite_id == suite.id).all()
    return {"id": suite.id, "name": suite.name, "description": suite.description, "fixture_hash": suite.fixture_hash, "dataset": suite.dataset, "cases": [{"id": case.id, "case_key": case.case_key, "name": case.name, "category": case.category, "difficulty": case.difficulty, "prompt": case.prompt, "target_type": case.target_type, "target_id": case.target_id, "reference_sql": case.reference_sql, "expected_tools": case.expected_tools, "expect_report": case.expect_report, "acceptance": case.acceptance} for case in cases]}


@app.post("/eval-suites/{suite_id}/cases")
def create_eval_case(suite_id: str, payload: dict, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    suite = db.query(EvalSuite).filter(EvalSuite.id == suite_id, EvalSuite.user_id == user.id).first()
    if not suite: raise HTTPException(404, "Evaluation suite not found")
    target_type, target_id = str(payload.get("target_type", "")), str(payload.get("target_id", ""))
    if target_type not in {"file", "connection"} or not target_id: raise HTTPException(422, "target_type must be file or connection and target_id is required")
    owned = db.query(UploadedFile).filter(UploadedFile.id == target_id, UploadedFile.user_id == user.id).first() if target_type == "file" else db.query(DataSource).filter(DataSource.id == target_id, DataSource.user_id == user.id).first()
    if not owned: raise HTTPException(404, "Evaluation resource not found")
    expected_tools = list(payload.get("expected_tools", []))
    expect_report = bool(payload.get("expect_report", False))
    acceptance = dict(payload.get("acceptance") or {"data_match": "exact_rows_ignore_order", "required_tools": expected_tools, "expect_report": expect_report, "evidence_required": True, "max_tool_calls": 4})
    case = EvalCase(suite_id=suite.id, case_key=str(payload.get("case_key", ""))[:80] or None, name=str(payload.get("name", "Untitled"))[:160], category=str(payload.get("category", "manual"))[:80], difficulty=str(payload.get("difficulty", "unrated"))[:24], prompt=str(payload.get("prompt", "")), target_type=target_type, target_id=target_id, reference_sql=str(payload.get("reference_sql", "")), expected_tools=expected_tools, expect_report=expect_report, acceptance=acceptance)
    if not case.prompt.strip() or not case.reference_sql.strip(): raise HTTPException(422, "prompt and reference_sql are required")
    db.add(case); db.commit(); db.refresh(case); return {"id": case.id, "name": case.name}


@app.post("/eval-suites/{suite_id}/runs")
def start_eval_suite(suite_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    suite = db.query(EvalSuite).filter(EvalSuite.id == suite_id, EvalSuite.user_id == user.id).first()
    if not suite: raise HTTPException(404, "Evaluation suite not found")
    run = EvalRun(suite_id=suite.id, user_id=user.id); db.add(run); db.commit(); db.refresh(run); run_eval.send(run.id)
    return {"id": run.id, "status": run.status}


@app.get("/eval-runs/{eval_run_id}")
def get_eval_run(eval_run_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    run = db.query(EvalRun).filter(EvalRun.id == eval_run_id, EvalRun.user_id == user.id).first()
    if not run: raise HTTPException(404, "Evaluation run not found")
    rows = db.query(EvalCaseResult).filter(EvalCaseResult.eval_run_id == run.id).all()
    return {"id": run.id, "status": run.status, "summary": run.summary, "error": run.error, "created_at": run.created_at, "completed_at": run.completed_at, "results": [{"case_id": item.eval_case_id, "agent_run_id": item.agent_run_id, "status": item.status, "passed": item.passed, "details": item.details} for item in rows]}


@app.post("/runs/{run_id}/replay")
def replay_agent_run(run_id: str, payload: dict, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    if payload.get("confirmed") is not True:
        raise HTTPException(422, "Replay requires confirmed=true because it invokes the configured model and may incur cost")
    source = db.query(AgentRun).filter(AgentRun.id == run_id, AgentRun.user_id == user.id).first()
    if not source: raise HTTPException(404, "Run not found")
    file_ids, connection_ids = list(source.resources.get("file_ids", [])), list(source.resources.get("connection_ids", []))
    files = db.query(UploadedFile).filter(UploadedFile.user_id == user.id, UploadedFile.id.in_(file_ids)).count() if file_ids else 0
    sources = db.query(DataSource).filter(DataSource.user_id == user.id, DataSource.id.in_(connection_ids)).count() if connection_ids else 0
    if files != len(file_ids) or sources != len(connection_ids): raise HTTPException(409, "Replay resources are no longer available")
    chat = Chat(user_id=user.id, title=f"Replay: {source.prompt[:180]}"); db.add(chat); db.commit(); db.refresh(chat)
    message = Message(chat_id=chat.id, role="user", content=source.prompt); db.add(message); db.flush(); save_parts(db, message, source.prompt); db.commit()
    sample_tables = list(source.resources.get("jaffle_shop_tables", []))
    replay = start_run(db, user.id, chat.id, message.id, source.prompt, file_ids, connection_ids, sample_tables, parent_run_id=source.id)
    answer, events = run_agent(db, user, chat, source.prompt, file_ids, connection_ids, lambda _kind, _data: None, replay, sample_tables)
    verifier = verify_run(db, replay, answer, events)
    assistant = Message(chat_id=chat.id, role="assistant", content=answer, tool_events=events); db.add(assistant); db.flush(); save_parts(db, assistant, answer, events); db.commit()
    return {"chat_id": chat.id, "run": serialize_run(db, replay), "answer": answer, "verifier": verifier}


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


@app.post("/connections/{connection_id}/sync-context")
def sync_connection_context_documents(connection_id: str, profile: bool = True, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    source = _source_for_user(db, user.id, connection_id)
    summary = sync_connection_context(source, profile=profile)
    source.schema_cache = {"tables": [item["table"] for item in summary["tables"]], "stored_as": "context_files"}
    source.context_synced_at = datetime.now(UTC)
    db.commit()
    return summary


@app.post("/files/{file_id}/sync-context")
def sync_file_context_documents(file_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    return sync_file_context(_file_for_user(db, user.id, file_id))


@app.post("/sample-tables/jaffle-shop/sync-context")
def sync_sample_context_documents(user: User = Depends(current_user)) -> dict:
    return sync_sample_context(user.id)


@app.get("/context/databases")
def list_context_databases(user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    targets = [context_store.connection_target(item) for item in db.query(DataSource).filter(DataSource.user_id == user.id).all()]
    targets += [context_store.file_target(item) for item in db.query(UploadedFile).filter(UploadedFile.user_id == user.id).all()]
    targets.append(context_store.sample_target(user.id))
    return [_context_database_summary(target) for target in targets]


@app.get("/context/{kind}/{asset_id}/tables")
def list_context_tables(kind: str, asset_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    target = _context_target_for_user(db, user, kind, asset_id)
    return {"target": target.reference, "tables": context_store.list_tables(target)}


@app.get("/context/{kind}/{asset_id}/tables/{table}/{document_kind}")
def read_context_table_document(kind: str, asset_id: str, table: str, document_kind: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    target = _context_target_for_user(db, user, kind, asset_id)
    content = context_store.read_table_document(target, table, document_kind)
    if content is None:
        raise HTTPException(404, "Context document not found")
    return {"target": target.reference, "table": table, "kind": document_kind, "content": content}


@app.get("/context/{kind}/{asset_id}/rules")
def read_context_rules(kind: str, asset_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    target = _context_target_for_user(db, user, kind, asset_id)
    return {"target": target.reference, "content": context_store.read_rules(target) or ""}


@app.put("/context/{kind}/{asset_id}/rules")
def write_context_rules(kind: str, asset_id: str, payload: dict, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    target = _context_target_for_user(db, user, kind, asset_id)
    context_store.write_rules(target, str(payload.get("content", "")))
    return {"target": target.reference, "content": context_store.read_rules(target) or ""}


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
def list_reports(chat_id: str | None = None, user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    query = db.query(Report).filter(Report.user_id == user.id, Report.archived_at.is_(None))
    if chat_id is not None:
        query = query.filter(Report.chat_id == chat_id)
    return [{"id": item.id, "title": item.title, "chat_id": item.chat_id, "updated_at": item.updated_at} for item in query.order_by(Report.updated_at.desc()).all()]


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


def _file_for_user(db: Session, user_id: str, file_id: str) -> UploadedFile:
    file = db.query(UploadedFile).filter(UploadedFile.id == file_id, UploadedFile.user_id == user_id).first()
    if not file:
        raise HTTPException(404, "File not found")
    return file


def _context_target_for_user(db: Session, user: User, kind: str, asset_id: str) -> ContextTarget:
    """Context is addressed only through a database the caller owns, so reads follow ownership."""
    if kind == "connection":
        return context_store.connection_target(_source_for_user(db, user.id, asset_id))
    if kind == "file":
        return context_store.file_target(_file_for_user(db, user.id, asset_id))
    if kind == "sample" and asset_id == context_store.SAMPLE_ASSET_ID:
        return context_store.sample_target(user.id)
    raise HTTPException(404, "Context target not found")


def _context_database_summary(target: ContextTarget) -> dict:
    tables = context_store.list_tables(target)
    rules = context_store.read_rules(target)
    return {"kind": target.kind, "asset_id": target.asset_id, "target": target.reference, "label": target.label, "table_count": len(tables), "has_rules": bool(rules and rules.strip())}


def _sse(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"
