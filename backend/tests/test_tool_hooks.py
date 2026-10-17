import pytest
from fastapi import HTTPException

from app.models import Chat, User
from app.services.tools import ToolContext, registry
from app.services.tools.errors import AUTO_RETRY, MODEL_MAY_RETRY, ErrorCode, error_from_http_exception, tool_error
from app.services.tools.hooks import (
    CallBudgetHook,
    DeadlineHook,
    OutputGuardHook,
    PolicyDenyHook,
    RetryHook,
    SchemaHook,
    ToolInvocation,
    compose,
)
from app.services.tools.policy import Budget, HookDecision, ToolPolicy
from app.services.tools.registry import Tool, ToolRegistry


def _policy(**overrides) -> ToolPolicy:
    defaults = {
        "max_tool_calls": 16,
        "max_iterations": 8,
        "turn_seconds": 180,
        "max_output_bytes": 24_000,
        "max_auto_retries": 1,
        "denied_tools": frozenset(),
        "redacted_keys": frozenset({"password", "api_key"}),
        "approval_required": frozenset(),
    }
    return ToolPolicy(**(defaults | overrides))


def _context(policy: ToolPolicy | None = None, **overrides) -> ToolContext:
    defaults = {
        "db": None,
        "user": User(id="user-1"),
        "chat": Chat(id="chat-1"),
        "budget": Budget(policy=policy or _policy()),
    }
    return ToolContext(**(defaults | overrides))


def _tool(handler, *, name="probe", properties=None, required=None, **overrides) -> Tool:
    parameters = {"type": "object", "properties": properties or {}}
    if required:
        parameters["required"] = required
    defaults = {"name": name, "description": "probe tool", "parameters": parameters, "handler": handler}
    return Tool(**(defaults | overrides))


def _run(hooks, tool: Tool, args: dict, context: ToolContext) -> dict:
    local = ToolRegistry(hooks=hooks)
    local.register(tool)
    return local.dispatch(tool.name, args, context)


# --- SchemaHook -------------------------------------------------------------


def test_missing_required_argument_is_refused_without_running_the_tool():
    calls = []
    tool = _tool(lambda args, ctx: calls.append(args) or {"ok": True}, properties={"target": {"type": "string"}}, required=["target"])

    result = _run((SchemaHook(),), tool, {}, _context())

    assert calls == []
    assert result["code"] == str(ErrorCode.INVALID_INPUT)
    assert "missing required argument 'target'" in result["error"]
    assert result["retryable"] is True


def test_execute_sql_without_target_returns_an_error_instead_of_raising_keyerror():
    """The crash class this layer exists to close: the real tool indexes args["target"]."""
    result = registry.dispatch("execute_sql", {"sql": "SELECT 1"}, _context())

    assert result["code"] == str(ErrorCode.INVALID_INPUT)
    assert "target" in result["error"]


def test_unknown_arguments_are_dropped_and_recorded():
    seen = {}
    tool = _tool(lambda args, ctx: seen.update(args) or {"ok": True}, properties={"target": {"type": "string"}})
    context = _context()

    result = _run((SchemaHook(),), tool, {"target": "data", "hallucinated": 1}, context)

    assert result == {"ok": True}
    assert seen == {"target": "data"}
    assert [(d.action, d.detail["dropped"]) for d in context.budget.trail] == [("mutate_args", ["hallucinated"])]


def test_arguments_are_coerced_to_their_declared_types():
    seen = {}
    tool = _tool(
        lambda args, ctx: seen.update(args) or {"ok": True},
        properties={"limit": {"type": "integer"}, "keys": {"type": "array", "items": {"type": "string"}}, "name": {"type": "string"}},
    )

    _run((SchemaHook(),), tool, {"limit": "5", "keys": "revenue", "name": 7}, _context())

    assert seen == {"limit": 5, "keys": ["revenue"], "name": "7"}


def test_enum_values_outside_the_declared_set_are_refused():
    tool = _tool(lambda args, ctx: {"ok": True}, properties={"kind": {"type": "string", "enum": ["columns", "profiling"]}})

    result = _run((SchemaHook(),), tool, {"kind": "credentials"}, _context())

    assert result["code"] == str(ErrorCode.INVALID_INPUT)
    assert "must be one of" in result["error"]


def test_a_non_integer_is_refused_rather_than_silently_coerced():
    tool = _tool(lambda args, ctx: {"ok": True}, properties={"limit": {"type": "integer"}})

    result = _run((SchemaHook(),), tool, {"limit": "many"}, _context())

    assert result["code"] == str(ErrorCode.INVALID_INPUT)


# --- CallBudgetHook and DeadlineHook ---------------------------------------


def test_the_call_budget_is_spent_and_then_refuses():
    tool = _tool(lambda args, ctx: {"ok": True})
    local = ToolRegistry(hooks=(CallBudgetHook(),))
    local.register(tool)
    context = _context(_policy(max_tool_calls=2))

    assert local.dispatch("probe", {}, context) == {"ok": True}
    assert local.dispatch("probe", {}, context) == {"ok": True}
    denied = local.dispatch("probe", {}, context)

    assert denied["code"] == str(ErrorCode.POLICY_VIOLATION)
    assert context.budget.tool_calls == 2
    assert [d.action for d in context.budget.trail] == ["deny"]


def test_a_turn_ending_tool_is_never_denied_by_budget_or_deadline():
    """Ending the turn is the cheapest action available; refusing it strands the user."""
    tool = _tool(lambda args, ctx: {"question": "which quarter?"}, name="clarify", ends_turn=True)
    context = _context(_policy(max_tool_calls=0, turn_seconds=-1))

    result = _run((DeadlineHook(), CallBudgetHook()), tool, {}, context)

    assert result == {"question": "which quarter?"}
    assert context.budget.trail == []


def test_an_expired_deadline_refuses_without_running_the_tool():
    calls = []
    tool = _tool(lambda args, ctx: calls.append(1) or {"ok": True})

    result = _run((DeadlineHook(),), tool, {}, _context(_policy(turn_seconds=-1)))

    assert calls == []
    assert result["code"] == str(ErrorCode.POLICY_VIOLATION)


def test_a_denied_tool_is_refused_with_permission_denied():
    tool = _tool(lambda args, ctx: {"ok": True}, name="export_result")

    result = _run((PolicyDenyHook(),), tool, {}, _context(_policy(denied_tools=frozenset({"export_result"}))))

    assert result["code"] == str(ErrorCode.PERMISSION_DENIED)
    assert "retryable" not in result


# --- RetryHook --------------------------------------------------------------


def test_transient_failures_are_retried_up_to_the_limit():
    attempts = []

    def flaky(args, ctx):
        attempts.append(len(attempts) + 1)
        if len(attempts) == 1:
            return tool_error(ErrorCode.RETRYABLE, "connection reset")
        return {"ok": True, "attempts": len(attempts)}

    context = _context(_policy(max_auto_retries=1))
    result = _run((RetryHook(),), _tool(flaky), {}, context)

    assert result == {"ok": True, "attempts": 2}
    assert [d.action for d in context.budget.trail] == ["retry"]


def test_invalid_input_is_never_auto_retried():
    """Re-sending identical arguments fails identically; retrying only burns budget."""
    attempts = []

    def failing(args, ctx):
        attempts.append(1)
        return tool_error(ErrorCode.INVALID_INPUT, "missing required argument 'target'")

    context = _context(_policy(max_auto_retries=3))
    result = _run((RetryHook(),), _tool(failing), {}, context)

    assert len(attempts) == 1
    assert result["code"] == str(ErrorCode.INVALID_INPUT)
    assert context.budget.trail == []


def test_retries_spend_call_budget_so_a_retry_storm_terminates():
    attempts = []

    def always_transient(args, ctx):
        attempts.append(1)
        return tool_error(ErrorCode.RETRYABLE, "connection reset")

    context = _context(_policy(max_auto_retries=5, max_tool_calls=3))
    _run((RetryHook(), CallBudgetHook()), _tool(always_transient), {}, context)

    assert len(attempts) == 3
    assert context.budget.tool_calls == 3


# --- OutputGuardHook --------------------------------------------------------


def test_oversized_row_lists_are_truncated_with_a_pointer_to_the_rest():
    rows = [{"id": index, "name": f"customer-{index}"} for index in range(400)]
    tool = _tool(lambda args, ctx: {"query_id": "q1", "rows": rows})
    context = _context(_policy(max_output_bytes=2_000))

    result = _run((OutputGuardHook(),), tool, {}, context)

    assert result["truncated"] is True
    assert len(result["rows"]) < len(rows)
    assert result["omitted_rows"] == len(rows) - len(result["rows"])
    assert result["query_id"] == "q1"
    assert [d.action for d in context.budget.trail] == ["truncate"]


def test_oversized_text_is_truncated_when_there_is_no_list_to_shrink():
    tool = _tool(lambda args, ctx: {"table": "orders", "content": "x" * 5_000})

    result = _run((OutputGuardHook(),), tool, {}, _context(_policy(max_output_bytes=1_000)))

    assert result["truncated"] is True
    assert len(result["content"]) < 5_000
    assert result["table"] == "orders"


def test_sensitive_keys_are_redacted_by_name_at_any_depth():
    tool = _tool(lambda args, ctx: {"rows": [{"user": "a", "password": "hunter2"}], "api_key": "sk-123"})
    context = _context()

    result = _run((OutputGuardHook(),), tool, {}, context)

    assert result["api_key"] == "[redacted]"
    assert result["rows"][0]["password"] == "[redacted]"
    assert result["rows"][0]["user"] == "a"
    assert [d.action for d in context.budget.trail] == ["redact"]


def test_document_text_is_never_rewritten_by_pattern_matching():
    """read_context returns arbitrary user documents; a regex would mangle them."""
    content = "The password column is named pwd_hash. Set api_key in the env."
    tool = _tool(lambda args, ctx: {"name": "rules", "content": content})

    result = _run((OutputGuardHook(),), tool, {}, _context())

    assert result["content"] == content


# --- Chain mechanics --------------------------------------------------------


def test_hooks_run_outermost_first_and_unwind_in_reverse():
    order = []

    def recorder(label):
        def hook(invocation, call_next):
            order.append(f"enter-{label}")
            result = call_next(invocation)
            order.append(f"exit-{label}")
            return result

        hook.name = label
        return hook

    chain = compose((recorder("a"), recorder("b")), lambda invocation: order.append("handler") or {"ok": True})
    chain(ToolInvocation(tool=_tool(lambda args, ctx: {}), args={}, context=_context()))

    assert order == ["enter-a", "enter-b", "handler", "exit-b", "exit-a"]


def test_a_hook_cannot_re_enter_dispatch():
    local = ToolRegistry()
    local.register(_tool(lambda args, ctx: {"ok": True}, name="inner"))
    local.register(_tool(lambda args, ctx: local.dispatch("inner", {}, ctx), name="outer"))

    with pytest.raises(RuntimeError, match="Nested tool dispatch"):
        local.dispatch("outer", {}, _context())


def test_the_default_chain_order_is_the_declared_one():
    assert [hook.name for hook in registry.hooks] == [
        "retry",
        "deadline",
        "call_budget",
        "policy_deny",
        "approval",
        "schema",
        "output_guard",
    ]


# --- Error taxonomy ---------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected"),
    [(400, ErrorCode.INVALID_INPUT), (403, ErrorCode.PERMISSION_DENIED), (404, ErrorCode.RESOURCE_UNAVAILABLE), (500, ErrorCode.RETRYABLE)],
)
def test_http_exceptions_map_to_the_taxonomy(status, expected):
    result = error_from_http_exception(HTTPException(status, "boom"))
    assert result["code"] == str(expected)


def test_only_model_retryable_codes_carry_the_retryable_flag():
    for code in ErrorCode:
        payload = tool_error(code, "boom")
        assert ("retryable" in payload) is (code in MODEL_MAY_RETRY), code


def test_auto_retry_is_narrower_than_what_the_model_may_retry():
    assert AUTO_RETRY < MODEL_MAY_RETRY
    assert ErrorCode.INVALID_INPUT in MODEL_MAY_RETRY
    assert ErrorCode.INVALID_INPUT not in AUTO_RETRY


def test_hook_decisions_are_drained_once():
    budget = Budget(policy=_policy())
    budget.record(HookDecision(hook="h", tool="t", action="deny"))

    assert len(budget.drain()) == 1
    assert budget.drain() == []


# --- Observability ----------------------------------------------------------


def test_hook_decisions_are_persisted_as_agent_steps():
    from dataclasses import replace

    from app.db import Base, SessionLocal, engine
    from app.models import AgentStep
    from app.services.agent import _record_hook_decisions
    from app.services.runs import start_run

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        user = User(id="u-hook", email="hook@example.com", password_hash="x")
        chat = Chat(id="c-hook", user_id="u-hook")
        db.add_all([user, chat])
        db.commit()
        run = start_run(db, user.id, chat.id, None, "export it", [], [])
        policy = replace(ToolPolicy.from_settings(), denied_tools=frozenset({"export_result"}))
        context = ToolContext(db=db, user=user, chat=chat, budget=Budget(policy=policy))

        result = registry.dispatch("export_result", {"query_id": "q1"}, context)
        _record_hook_decisions(db, run, context, "export_result")

        assert result["code"] == str(ErrorCode.PERMISSION_DENIED)
        steps = db.query(AgentStep).filter(AgentStep.run_id == run.id, AgentStep.kind == "hook").all()
        assert [step.name for step in steps] == ["policy_deny"]
        assert steps[0].output == {"action": "deny", "code": str(ErrorCode.PERMISSION_DENIED)}
        assert steps[0].status == "failed"
        assert context.budget.trail == []
