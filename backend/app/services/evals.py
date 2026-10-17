import json
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from ..models import AgentRun, Chat, DataSource, EvalCase, EvalCaseResult, EvalRun, EvalSuite, Message, QueryResult, UploadedFile, User
from .agent import run_agent
from .tools import ToolContext, registry
from .conversation import save_parts
from .runs import start_run, verify_run


def _normalized(rows: list[dict]) -> list[str]:
    return sorted(json.dumps(row, ensure_ascii=False, sort_keys=True, default=str) for row in rows)


def evaluate_case(
    case: EvalCase,
    actual_rows: list[dict] | None,
    expected_rows: list[dict],
    events: list[dict],
    verifier: dict,
) -> tuple[bool, dict]:
    acceptance = dict(case.acceptance or {})
    data_mode = acceptance.get("data_match", "exact_rows_ignore_order")
    data_match = bool(actual_rows is not None and data_mode == "exact_rows_ignore_order" and _normalized(actual_rows) == _normalized(expected_rows))
    event_names = [str(event.get("name", "")) for event in events]
    event_name_set = set(event_names)
    required_tools = set(acceptance.get("required_tools") or case.expected_tools or [])
    forbidden_tools = set(acceptance.get("forbidden_tools") or [])
    tools_match = required_tools.issubset(event_name_set)
    forbidden_tools_match = forbidden_tools.isdisjoint(event_name_set)
    expect_report = bool(acceptance.get("expect_report", case.expect_report))
    report_match = not expect_report or "create_report" in event_name_set
    evidence_required = bool(acceptance.get("evidence_required", True))
    evidence_ids = list(verifier.get("evidence_query_ids") or [])
    evidence_match = not evidence_required or bool(evidence_ids)
    max_tool_calls = int(acceptance.get("max_tool_calls", 4))
    tool_budget_match = len(events) <= max_tool_calls
    passed = all((data_match, tools_match, forbidden_tools_match, report_match, evidence_match, tool_budget_match))
    return passed, {
        "data_match": data_match,
        "data_match_mode": data_mode,
        "tools_match": tools_match,
        "missing_tools": sorted(required_tools - event_name_set),
        "forbidden_tools_match": forbidden_tools_match,
        "used_forbidden_tools": sorted(forbidden_tools & event_name_set),
        "report_match": report_match,
        "evidence_match": evidence_match,
        "tool_budget_match": tool_budget_match,
        "tool_call_count": len(events),
        "max_tool_calls": max_tool_calls,
        "events": event_names,
    }


def run_suite(db: Session, eval_run: EvalRun) -> None:
    suite = db.get(EvalSuite, eval_run.suite_id); user = db.get(User, eval_run.user_id)
    if not suite or not user: raise ValueError("Evaluation suite or user no longer exists")
    eval_run.status = "running"; db.commit()
    # A run can be explicitly re-queued after an interrupted worker. Preserve
    # completed cases and continue only the missing cases.
    results = db.query(EvalCaseResult).filter(EvalCaseResult.eval_run_id == eval_run.id).all()
    completed_case_ids = {result.eval_case_id for result in results}
    for case in db.query(EvalCase).filter(EvalCase.suite_id == suite.id).all():
        if case.id in completed_case_ids:
            continue
        result = _run_case(db, user, eval_run, case); results.append(result)
    passed = sum(1 for result in results if result.passed)
    total = db.query(EvalCase).filter(EvalCase.suite_id == suite.id).count()
    eval_run.status = "completed"; eval_run.completed_at = datetime.now(timezone.utc); eval_run.summary = {"total": total, "passed": passed, "failed": total - passed}; db.commit()


def _run_case(db: Session, user: User, eval_run: EvalRun, case: EvalCase) -> EvalCaseResult:
    chat = Chat(user_id=user.id, title=f"Eval: {case.name}"); db.add(chat); db.commit(); db.refresh(chat)
    file_ids, connection_ids = ([case.target_id], []) if case.target_type == "file" else ([], [case.target_id])
    file = db.query(UploadedFile).filter(UploadedFile.id == case.target_id, UploadedFile.user_id == user.id).first() if case.target_type == "file" else None
    source = db.query(DataSource).filter(DataSource.id == case.target_id, DataSource.user_id == user.id).first() if case.target_type == "connection" else None
    if (case.target_type == "file" and not file) or (case.target_type == "connection" and not source):
        record = EvalCaseResult(eval_run_id=eval_run.id, eval_case_id=case.id, status="blocked", passed=False, details={"reason": "Referenced resource is unavailable"}); db.add(record); db.commit(); return record
    reference_context = ToolContext(db=db, user=user, chat=chat, files=[file] if file else [], sources=[source] if source else [])
    expected = registry.dispatch("execute_sql", {"target": "data" if file else source.id, "sql": case.reference_sql}, reference_context)
    if expected.get("error"):
        record = EvalCaseResult(eval_run_id=eval_run.id, eval_case_id=case.id, status="failed", passed=False, details={"reason": "Reference SQL failed", "error": expected["error"]}); db.add(record); db.commit(); return record
    message = Message(chat_id=chat.id, role="user", content=case.prompt); db.add(message); db.flush(); save_parts(db, message, case.prompt); db.commit()
    run = start_run(db, user.id, chat.id, message.id, case.prompt, file_ids, connection_ids)
    try:
        answer, events = run_agent(db, user, chat, case.prompt, file_ids, connection_ids, run)
        verifier = verify_run(db, run, answer, events)
        actual_ids = [event["output"].get("query_id") for event in events if event.get("name") == "execute_sql" and isinstance(event.get("output"), dict) and event["output"].get("query_id")]
        actual = db.get(QueryResult, actual_ids[-1]) if actual_ids else None
        expected_rows = db.get(QueryResult, expected["query_id"]).rows
        passed, checks = evaluate_case(case, actual.rows if actual else None, expected_rows, events, verifier)
        details = {**checks, "expected_query_id": expected["query_id"], "actual_query_id": actual.id if actual else None, "verifier": verifier, "answer": answer}
        record = EvalCaseResult(eval_run_id=eval_run.id, eval_case_id=case.id, agent_run_id=run.id, status="passed" if passed else "failed", passed=passed, details=details)
    except Exception as exc:
        record = EvalCaseResult(eval_run_id=eval_run.id, eval_case_id=case.id, agent_run_id=run.id, status="failed", passed=False, details={"error": str(exc)})
    db.add(record); db.commit(); return record
