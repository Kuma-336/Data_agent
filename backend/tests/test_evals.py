from app.db import Base, SessionLocal, engine
import importlib.util
from pathlib import Path

from app.models import DataSource, EvalCase, EvalCaseResult, EvalRun, EvalSuite, User
from app.services.eval_fixtures import import_fixture, validate_fixture
from app.services.evals import evaluate_case, run_suite


ROOT = Path(__file__).resolve().parents[2]


def _load_generator():
    path = ROOT / "scripts" / "generate_northwind_eval_cases.py"
    spec = importlib.util.spec_from_file_location("northwind_eval_generator", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def setup_function():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def test_run_suite_resumes_without_repeating_completed_cases():
    with SessionLocal() as db:
        user = User(email="eval-resume@example.com", password_hash="not-used")
        db.add(user)
        db.flush()
        suite = EvalSuite(user_id=user.id, name="Resume suite")
        db.add(suite)
        db.flush()
        case = EvalCase(
            suite_id=suite.id,
            name="Already complete",
            prompt="This must not call the model",
            target_type="connection",
            target_id="missing-but-not-used",
            reference_sql="SELECT 1",
            expected_tools=["execute_sql"],
            expect_report=False,
        )
        db.add(case)
        db.flush()
        run = EvalRun(suite_id=suite.id, user_id=user.id, status="running")
        db.add(run)
        db.flush()
        db.add(EvalCaseResult(
            eval_run_id=run.id,
            eval_case_id=case.id,
            status="passed",
            passed=True,
            details={"data_match": True},
        ))
        db.commit()

        run_suite(db, run)

        assert db.query(EvalCaseResult).filter(EvalCaseResult.eval_run_id == run.id).count() == 1
        assert run.status == "completed"
        assert run.summary == {"total": 1, "passed": 1, "failed": 0}


def test_northwind_fixture_is_deterministic_and_valid():
    generator = _load_generator()
    payload = generator.build_payload()
    fixture_bytes = generator.canonical_json(payload)
    smoke_bytes = generator.canonical_json(generator.build_smoke_payload(payload))
    lock = generator.build_lock(payload, fixture_bytes, smoke_bytes)

    assert validate_fixture(payload) == []
    assert generator.validate_payload(payload) == []
    assert fixture_bytes == (ROOT / "evals" / "northwind_baseline_v1.json").read_bytes()
    assert smoke_bytes == (ROOT / "evals" / "northwind_baseline_v1_smoke.json").read_bytes()
    assert generator.canonical_json(lock) == (ROOT / "evals" / "northwind_baseline_v1.lock.json").read_bytes()
    assert lock["case_count"] == 150
    assert lock["category_counts"]["report"] == 10
    assert len(generator.build_smoke_payload(payload)["cases"]) == 12


def test_fixture_import_is_idempotent_for_the_same_target():
    generator = _load_generator()
    payload = generator.build_payload()
    with SessionLocal() as db:
        user = User(email="fixture@example.com", password_hash="not-used")
        db.add(user)
        db.flush()
        source = DataSource(
            user_id=user.id,
            name="Northwind Baseline",
            host="localhost",
            database_name="northwind",
            username="northwind_reader",
            encrypted_password="not-used",
        )
        db.add(source)
        db.commit()

        suite, imported = import_fixture(db, user, payload, source.id)
        same_suite, imported_again = import_fixture(db, user, payload, source.id)

        assert imported is True
        assert imported_again is False
        assert same_suite.id == suite.id
        assert db.query(EvalCase).filter(EvalCase.suite_id == suite.id).count() == 150
        first = db.query(EvalCase).filter(EvalCase.suite_id == suite.id, EvalCase.case_key == "NW001").one()
        assert first.category == "simple_filter"
        assert first.acceptance["max_tool_calls"] == 4


def test_evaluate_case_enforces_evidence_forbidden_tools_and_budget():
    case = EvalCase(
        suite_id="suite",
        case_key="T001",
        name="Test",
        prompt="Test",
        target_type="connection",
        target_id="source",
        reference_sql="SELECT 1 AS value",
        expected_tools=["execute_sql"],
        expect_report=False,
        acceptance={
            "data_match": "exact_rows_ignore_order",
            "required_tools": ["execute_sql"],
            "forbidden_tools": ["create_report"],
            "expect_report": False,
            "evidence_required": True,
            "max_tool_calls": 1,
        },
    )
    passed, checks = evaluate_case(
        case,
        [{"value": 1}],
        [{"value": 1}],
        [{"name": "execute_sql"}, {"name": "create_report"}],
        {"evidence_query_ids": ["query-1"]},
    )

    assert passed is False
    assert checks["data_match"] is True
    assert checks["forbidden_tools_match"] is False
    assert checks["tool_budget_match"] is False
