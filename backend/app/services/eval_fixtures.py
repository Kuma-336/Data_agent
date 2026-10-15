import hashlib
import json

from sqlalchemy.orm import Session

from ..models import EvalCase, EvalSuite, User


SUPPORTED_DATA_MATCH = {"exact_rows_ignore_order"}
SUPPORTED_TARGET_TYPES = {"connection", "file"}


def fixture_hash(payload: dict) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def validate_fixture(payload: dict) -> list[str]:
    errors: list[str] = []
    if payload.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    suite = payload.get("suite")
    if not isinstance(suite, dict):
        errors.append("suite must be an object")
        return errors
    if not str(suite.get("name", "")).strip():
        errors.append("suite.name is required")
    if suite.get("target_type") not in SUPPORTED_TARGET_TYPES:
        errors.append("suite.target_type must be connection or file")

    cases = payload.get("cases")
    if not isinstance(cases, list) or not cases:
        errors.append("cases must be a non-empty array")
        return errors
    if len(cases) > 1000:
        errors.append("cases cannot contain more than 1000 entries")
        return errors

    seen_keys: set[str] = set()
    for index, case in enumerate(cases, start=1):
        prefix = f"cases[{index - 1}]"
        if not isinstance(case, dict):
            errors.append(f"{prefix} must be an object")
            continue
        case_key = str(case.get("case_key", "")).strip()
        if not case_key:
            errors.append(f"{prefix}.case_key is required")
        elif case_key in seen_keys:
            errors.append(f"{prefix}.case_key is duplicated")
        seen_keys.add(case_key)
        for field in ("name", "prompt", "reference_sql", "category", "difficulty"):
            if not str(case.get(field, "")).strip():
                errors.append(f"{prefix}.{field} is required")
        sql = str(case.get("reference_sql", "")).lstrip().upper()
        if sql and not (sql.startswith("SELECT ") or sql.startswith("WITH ")):
            errors.append(f"{prefix}.reference_sql must start with SELECT or WITH")
        expected_tools = case.get("expected_tools")
        if not isinstance(expected_tools, list) or not all(isinstance(tool, str) and tool for tool in expected_tools):
            errors.append(f"{prefix}.expected_tools must be an array of tool names")
        acceptance = case.get("acceptance")
        if not isinstance(acceptance, dict):
            errors.append(f"{prefix}.acceptance must be an object")
            continue
        if acceptance.get("data_match") not in SUPPORTED_DATA_MATCH:
            errors.append(f"{prefix}.acceptance.data_match is unsupported")
        max_tool_calls = acceptance.get("max_tool_calls")
        if not isinstance(max_tool_calls, int) or isinstance(max_tool_calls, bool) or max_tool_calls < 1:
            errors.append(f"{prefix}.acceptance.max_tool_calls must be a positive integer")
        if acceptance.get("required_tools") != expected_tools:
            errors.append(f"{prefix}.acceptance.required_tools must equal expected_tools")
        if bool(acceptance.get("expect_report")) != bool(case.get("expect_report")):
            errors.append(f"{prefix}.acceptance.expect_report must equal expect_report")
    return errors


def import_fixture(db: Session, user: User, payload: dict, target_id: str) -> tuple[EvalSuite, bool]:
    errors = validate_fixture(payload)
    if errors:
        raise ValueError("; ".join(errors))

    digest = fixture_hash(payload)
    existing = db.query(EvalSuite).filter(EvalSuite.user_id == user.id, EvalSuite.fixture_hash == digest).all()
    for suite in existing:
        first_case = db.query(EvalCase).filter(EvalCase.suite_id == suite.id).first()
        if first_case and first_case.target_id == target_id:
            return suite, False

    suite_payload = payload["suite"]
    suite = EvalSuite(
        user_id=user.id,
        name=str(suite_payload["name"])[:160],
        description=str(suite_payload.get("description", ""))[:4000],
        fixture_hash=digest,
        dataset=dict(suite_payload.get("dataset") or {}),
    )
    db.add(suite)
    db.flush()
    target_type = str(suite_payload["target_type"])
    for case_payload in payload["cases"]:
        db.add(EvalCase(
            suite_id=suite.id,
            case_key=str(case_payload["case_key"])[:80],
            name=str(case_payload["name"])[:160],
            category=str(case_payload["category"])[:80],
            difficulty=str(case_payload["difficulty"])[:24],
            prompt=str(case_payload["prompt"]),
            target_type=target_type,
            target_id=target_id,
            reference_sql=str(case_payload["reference_sql"]),
            expected_tools=list(case_payload["expected_tools"]),
            expect_report=bool(case_payload.get("expect_report", False)),
            acceptance=dict(case_payload["acceptance"]),
        ))
    db.commit()
    db.refresh(suite)
    return suite, True
