from datetime import datetime, timezone
from time import perf_counter

from sqlalchemy.orm import Session

from ..config import settings
from ..models import AgentRun, AgentStep, Report, ReportBlock


def default_plan(file_ids: list[str], connection_ids: list[str], jaffle_shop_tables: list[str]) -> list[dict]:
    inspect = "Inspect attached file" if file_ids else "Inspect selected database schema" if connection_ids else "Inspect selected jaffle_shop tables"
    return [
        {"title": inspect, "status": "pending"},
        {"title": "Run guarded read-only analysis", "status": "pending"},
        {"title": "Explain findings with evidence", "status": "pending"},
        {"title": "Verify answer and report quality", "status": "pending"},
    ]


def start_run(db: Session, user_id: str, chat_id: str, message_id: str | None, prompt: str, file_ids: list[str], connection_ids: list[str], jaffle_shop_tables: list[str] | None = None, parent_run_id: str | None = None) -> AgentRun:
    jaffle_shop_tables = jaffle_shop_tables or []
    run = AgentRun(user_id=user_id, chat_id=chat_id, user_message_id=message_id, parent_run_id=parent_run_id, prompt=prompt, resources={"file_ids": file_ids, "connection_ids": connection_ids, "jaffle_shop_tables": jaffle_shop_tables}, plan=default_plan(file_ids, connection_ids, jaffle_shop_tables), model=settings.llm_model)
    db.add(run); db.commit(); db.refresh(run)
    add_step(db, run, "plan", "execution_plan", {"resources": run.resources}, {"steps": run.plan})
    return run


def add_step(db: Session, run: AgentRun, kind: str, name: str, input_: dict | None = None, output: dict | None = None, error: str | None = None, duration_ms: int | None = None) -> AgentStep:
    order = db.query(AgentStep).filter(AgentStep.run_id == run.id).count() + 1
    step = AgentStep(run_id=run.id, step_order=order, kind=kind, name=name, input=input_, output=output, error=error, status="failed" if error else "completed", duration_ms=duration_ms)
    db.add(step); db.commit(); return step


def record_usage(db: Session, run: AgentRun, input_tokens: int, output_tokens: int) -> None:
    run.input_tokens += input_tokens; run.output_tokens += output_tokens; db.commit()


def verify_run(db: Session, run: AgentRun, answer: str, events: list[dict]) -> dict:
    query_events = [event for event in events if event.get("name") == "execute_sql" and isinstance(event.get("output"), dict) and event["output"].get("query_id")]
    tool_errors = [event["name"] for event in events if isinstance(event.get("output"), dict) and event["output"].get("error")]
    reports = db.query(Report).filter(Report.user_id == run.user_id, Report.chat_id == run.chat_id).all()
    report_has_narrative = any(db.query(ReportBlock).filter(ReportBlock.report_id == report.id, ReportBlock.block_type == "markdown").count() > 0 for report in reports)
    warnings: list[str] = []
    if not query_events: warnings.append("No query result was produced; factual claims may need evidence.")
    if tool_errors: warnings.append(f"Tool errors: {', '.join(tool_errors)}")
    if reports and not report_has_narrative: warnings.append("A report was created without a markdown narrative.")
    if not answer.strip(): warnings.append("The agent returned no final answer.")
    verdict = {"status": "needs_review" if warnings else "pass", "evidence_query_ids": [event["output"]["query_id"] for event in query_events], "warnings": warnings, "checked": ["query evidence", "tool errors", "report narrative", "final answer"]}
    run.verifier = verdict; run.status = "completed" if not warnings else "needs_review"; run.completed_at = datetime.now(timezone.utc); db.commit()
    add_step(db, run, "verifier", "deterministic_verifier", {"answer_length": len(answer), "tool_events": len(events)}, verdict)
    return verdict


def serialize_run(db: Session, run: AgentRun) -> dict:
    steps = db.query(AgentStep).filter(AgentStep.run_id == run.id).order_by(AgentStep.step_order).all()
    return {"id": run.id, "chat_id": run.chat_id, "parent_run_id": run.parent_run_id, "status": run.status, "prompt": run.prompt, "resources": run.resources, "plan": run.plan, "verifier": run.verifier, "model": run.model, "input_tokens": run.input_tokens, "output_tokens": run.output_tokens, "started_at": run.started_at, "completed_at": run.completed_at, "steps": [{"id": step.id, "order": step.step_order, "kind": step.kind, "name": step.name, "status": step.status, "input": step.input, "output": step.output, "error": step.error, "duration_ms": step.duration_ms, "created_at": step.created_at} for step in steps]}
