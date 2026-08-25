"""Streamable HTTP MCP facade for Personal Data Agent.

Every operation is executed with the user resolved from the bearer token, so
resource IDs from another user cannot be used to retrieve data or alter reports.
"""
import json
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from ..config import settings
from ..models import Chat, DataSource, Message, ProjectContextDocument, Report, UploadedFile, User
from .agent import _execute_tool, run_agent
from .conversation import save_parts
from .reports import save_report, serialize_report


def tool_definitions() -> list[dict]:
    tools: list[dict] = []
    if settings.mcp_subagent_enabled:
        tools.append(_tool("ask_data_agent", "Ask the full governed data-analysis agent. It uses saved context, executes guarded read-only SQL, and records the conversation.", {
            "question": _string("The analytics question"), "chat_id": _string("Optional previous chat id to continue"),
            "file_ids": _array("Uploaded file ids to attach"), "connection_ids": _array("Saved connection ids to attach"),
        }, ["question"]))
    if settings.mcp_context_enabled:
        tools.extend([
            _tool("execute_sql", "Run one guarded read-only SELECT/WITH query against an uploaded file or saved PostgreSQL connection.", {"sql": _string("SQL statement"), "connection_id": _string("Saved connection id"), "file_id": _string("Uploaded file id")}, ["sql"]),
            _tool("grep", "Search business rules, data dictionaries, and metadata documents.", {"query": _string("Case-insensitive text to search")}, ["query"]),
            _tool("ls", "List available context documents, uploaded files, and saved database connections.", {"path": _string("Optional: context, files, or connections")}),
        ])
    if settings.mcp_reports_enabled:
        tools.extend([
            _tool("list_reports", "List active reports."),
            _tool("get_report", "Get a report and its blocks.", {"report_id": _string("Report id")}, ["report_id"]),
            _tool("create_report", "Create a report from markdown, metric, query_table, chart, or divider blocks.", {"title": _string("Report title"), "blocks": {"type": "array", "items": {"type": "object"}}, "chat_id": _string("Optional source chat id")}, ["title", "blocks"]),
            _tool("update_report", "Replace a report's title and blocks.", {"report_id": _string("Report id"), "title": _string("Report title"), "blocks": {"type": "array", "items": {"type": "object"}}}, ["report_id", "title", "blocks"]),
            _tool("archive_report", "Archive a report; it remains stored but no longer appears in normal lists.", {"report_id": _string("Report id")}, ["report_id"]),
        ])
    return tools


def call_tool(db: Session, user: User, name: str, arguments: dict) -> dict:
    if name == "ask_data_agent" and settings.mcp_subagent_enabled:
        return _ask_agent(db, user, arguments)
    if name == "execute_sql" and settings.mcp_context_enabled:
        return _run_sql(db, user, arguments)
    if name == "grep" and settings.mcp_context_enabled:
        needle = str(arguments.get("query", "")).strip().lower()
        if not needle: raise ValueError("query is required")
        matches = []
        for doc in db.query(ProjectContextDocument).filter(ProjectContextDocument.user_id == user.id).all():
            position = doc.content.lower().find(needle)
            if needle in doc.name.lower() or position >= 0:
                matches.append({"name": doc.name, "path": doc.path, "excerpt": doc.content[max(0, position - 180): position + len(needle) + 320]})
        return {"matches": matches[:50]}
    if name == "ls" and settings.mcp_context_enabled:
        return _list_resources(db, user, str(arguments.get("path", "")).strip().lower())
    if settings.mcp_reports_enabled:
        return _report_tool(db, user, name, arguments)
    raise ValueError(f"Unknown or disabled tool: {name}")


def _ask_agent(db: Session, user: User, args: dict) -> dict:
    question = str(args.get("question", "")).strip()
    if not question: raise ValueError("question is required")
    chat_id = args.get("chat_id")
    chat = db.query(Chat).filter(Chat.id == chat_id, Chat.user_id == user.id, Chat.deleted_at.is_(None)).first() if chat_id else None
    if chat is None:
        if chat_id: raise ValueError("chat_id is not available")
        chat = Chat(user_id=user.id, title=question[:200]); db.add(chat); db.commit(); db.refresh(chat)
    files, connections = list(args.get("file_ids") or []), list(args.get("connection_ids") or [])
    message = Message(chat_id=chat.id, role="user", content=question)
    db.add(message); db.flush(); save_parts(db, message, question); db.commit()
    answer, events = run_agent(db, user, chat, question, files, connections, lambda _kind, _data: None)
    assistant = Message(chat_id=chat.id, role="assistant", content=answer, tool_events=events)
    db.add(assistant); db.flush(); save_parts(db, assistant, answer, events); db.commit()
    return {"chat_id": chat.id, "answer": answer, "tool_events": events, "chat_url": f"/chats/{chat.id}"}


def _run_sql(db: Session, user: User, args: dict) -> dict:
    sql, connection_id, file_id = str(args.get("sql", "")), args.get("connection_id"), args.get("file_id")
    if bool(connection_id) == bool(file_id): raise ValueError("Provide exactly one of connection_id or file_id")
    chat = Chat(user_id=user.id, title="MCP SQL query"); db.add(chat); db.commit(); db.refresh(chat)
    if file_id:
        file = db.query(UploadedFile).filter(UploadedFile.id == file_id, UploadedFile.user_id == user.id).first()
        if not file: raise ValueError("file_id is not available")
        return _execute_tool(db, user, chat, [file], [], "execute_sql", {"target": "data", "sql": sql})
    source = db.query(DataSource).filter(DataSource.id == connection_id, DataSource.user_id == user.id).first()
    if not source: raise ValueError("connection_id is not available")
    return _execute_tool(db, user, chat, [], [source], "execute_sql", {"target": connection_id, "sql": sql})


def _list_resources(db: Session, user: User, path: str) -> dict:
    if path not in {"", "context", "files", "connections"}: raise ValueError("path must be context, files, or connections")
    output: dict[str, list[dict]] = {}
    if path in {"", "context"}: output["context"] = [{"name": x.name, "path": x.path} for x in db.query(ProjectContextDocument).filter(ProjectContextDocument.user_id == user.id).all()]
    if path in {"", "files"}: output["files"] = [{"id": x.id, "name": x.original_name, "schema": x.file_schema} for x in db.query(UploadedFile).filter(UploadedFile.user_id == user.id).all()]
    if path in {"", "connections"}: output["connections"] = [{"id": x.id, "name": x.name, "host": x.host, "database": x.database_name, "schema": x.schema_cache} for x in db.query(DataSource).filter(DataSource.user_id == user.id).all()]
    return output


def _report_tool(db: Session, user: User, name: str, args: dict) -> dict:
    if name == "list_reports":
        rows = db.query(Report).filter(Report.user_id == user.id, Report.archived_at.is_(None)).order_by(Report.updated_at.desc()).all()
        return {"reports": [{"id": x.id, "title": x.title, "updated_at": x.updated_at} for x in rows]}
    if name == "create_report": return serialize_report(db, user, save_report(db, user, str(args["title"]), list(args["blocks"]), args.get("chat_id")))
    report = db.query(Report).filter(Report.id == args.get("report_id"), Report.user_id == user.id).first()
    if not report: raise ValueError("report_id is not available")
    if name == "get_report": return serialize_report(db, user, report)
    if name == "update_report": return serialize_report(db, user, save_report(db, user, str(args["title"]), list(args["blocks"]), report.chat_id, report))
    if name == "archive_report":
        report.archived_at = datetime.now(timezone.utc); db.commit(); return {"id": report.id, "archived": True}
    raise ValueError(f"Unknown or disabled tool: {name}")


def jsonrpc_result(request_id: object, result: dict, session_id: str | None = None) -> tuple[dict, dict]:
    return ({"jsonrpc": "2.0", "id": request_id, "result": result}, {"Mcp-Session-Id": session_id} if session_id else {})

def jsonrpc_error(request_id: object, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

def _tool(name: str, description: str, properties: dict | None = None, required: list[str] | None = None) -> dict:
    schema = {"type": "object", "properties": properties or {}}
    if required: schema["required"] = required
    return {"name": name, "description": description, "inputSchema": schema}

def _string(description: str) -> dict: return {"type": "string", "description": description}
def _array(description: str) -> dict: return {"type": "array", "description": description, "items": {"type": "string"}}
