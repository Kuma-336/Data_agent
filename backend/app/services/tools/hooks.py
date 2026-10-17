"""The tool-dispatch hook chain.

An onion: each hook may inspect and rewrite the invocation, short-circuit it, or
wrap the inner call to bound it or re-invoke it. Retry and timeout need that
wrapping, which an observer-style event bus structurally cannot provide.

Order is explicit and load-bearing; see DEFAULT_TOOL_HOOKS.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Protocol

from .errors import AUTO_RETRY, ErrorCode, error_code, tool_error
from .policy import Budget, HookDecision, invocation_fingerprint
from .registry import Tool, ToolContext

MAX_DISPATCH_DEPTH = 1


@dataclass(frozen=True)
class ToolInvocation:
    tool: Tool
    args: dict
    context: ToolContext
    attempt: int = 1

    @property
    def budget(self) -> Budget:
        return self.context.budget

    def with_args(self, args: dict) -> "ToolInvocation":
        return replace(self, args=args)

    def with_attempt(self, attempt: int) -> "ToolInvocation":
        return replace(self, attempt=attempt)


Next = Callable[[ToolInvocation], dict]


class ToolHook(Protocol):
    name: str

    def __call__(self, invocation: ToolInvocation, call_next: Next) -> dict: ...


def compose(hooks: Sequence[ToolHook], terminal: Next) -> Next:
    """Wrap `terminal` so that hooks[0] is the outermost layer."""
    chain = terminal
    for hook in reversed(tuple(hooks)):
        chain = _link(hook, chain)
    return chain


@dataclass(frozen=True)
class RetryHook:
    """Re-invokes transient infrastructure failures only.

    Outermost so that a retry re-checks the deadline and consumes call budget,
    which is what makes a retry storm terminate.
    """

    name: str = "retry"

    def __call__(self, invocation: ToolInvocation, call_next: Next) -> dict:
        limit = invocation.budget.policy.max_auto_retries
        result = call_next(invocation)
        attempt = invocation.attempt
        while attempt <= limit and error_code(result) in AUTO_RETRY:
            attempt += 1
            _record(invocation, self.name, "retry", detail={"attempt": attempt, "after": result.get("code")})
            result = call_next(invocation.with_attempt(attempt))
        return result


@dataclass(frozen=True)
class DeadlineHook:
    """Turn-level wall-clock ceiling, checked before each call.

    Per-call enforcement is deliberately not here: cancelling a running handler
    needs a thread that would keep using the same SQLAlchemy Session, which is
    not thread-safe. Real per-call limits belong to the resource (statement
    timeout, client timeout).
    """

    name: str = "deadline"

    def __call__(self, invocation: ToolInvocation, call_next: Next) -> dict:
        budget = invocation.budget
        if invocation.tool.ends_turn or budget.seconds_left > 0:
            return call_next(invocation)
        return _deny(
            invocation,
            self.name,
            ErrorCode.POLICY_VIOLATION,
            "This turn ran out of time. Answer with the results already gathered.",
            {"turn_seconds": budget.policy.turn_seconds},
        )


@dataclass(frozen=True)
class CallBudgetHook:
    name: str = "call_budget"

    def __call__(self, invocation: ToolInvocation, call_next: Next) -> dict:
        budget = invocation.budget
        if invocation.tool.ends_turn:
            return call_next(invocation)
        limit = budget.policy.max_tool_calls
        if budget.tool_calls >= limit:
            return _deny(
                invocation,
                self.name,
                ErrorCode.POLICY_VIOLATION,
                "The tool-call budget for this turn is spent. Answer with the results already gathered.",
                {"limit": limit, "observed": budget.tool_calls + 1},
            )
        budget.tool_calls += 1
        return call_next(invocation)


@dataclass(frozen=True)
class PolicyDenyHook:
    name: str = "policy_deny"

    def __call__(self, invocation: ToolInvocation, call_next: Next) -> dict:
        if invocation.tool.name in invocation.budget.policy.denied_tools:
            return _deny(
                invocation,
                self.name,
                ErrorCode.PERMISSION_DENIED,
                f"The tool '{invocation.tool.name}' is disabled for this deployment. Do not call it again.",
                {"tool": invocation.tool.name},
            )
        return call_next(invocation)


@dataclass(frozen=True)
class ApprovalHook:
    """Holds a high-risk call until the user has approved that exact call.

    The turn ends rather than suspending: the denial is recorded, the agent
    explains what needs approval, and a later turn carries the granted
    fingerprint in the budget.
    """

    name: str = "approval"

    def __call__(self, invocation: ToolInvocation, call_next: Next) -> dict:
        budget = invocation.budget
        if invocation.tool.name not in budget.policy.approval_required:
            return call_next(invocation)
        fingerprint = invocation_fingerprint(invocation.tool.name, invocation.args)
        if fingerprint in budget.approvals:
            return call_next(invocation)
        _record(
            invocation,
            self.name,
            "approval_required",
            code=str(ErrorCode.POLICY_VIOLATION),
            detail={"tool": invocation.tool.name, "fingerprint": fingerprint, "args": invocation.args},
        )
        return tool_error(
            ErrorCode.POLICY_VIOLATION,
            f"'{invocation.tool.name}' needs the user's approval before it can run. "
            "Stop here, tell the user what you intend to do and why, and wait for them to approve.",
            approval_required={"tool": invocation.tool.name, "fingerprint": fingerprint},
        )


@dataclass(frozen=True)
class SchemaHook:
    """Validates arguments against the tool's own declared schema.

    Forgiving where it is safe and strict where it is not: unknown keys are
    dropped (models hallucinate extras constantly, and rejecting burns a turn),
    while a missing or mistyped required key is refused.
    """

    name: str = "schema"

    def __call__(self, invocation: ToolInvocation, call_next: Next) -> dict:
        cleaned, dropped, problems = _validate(invocation.tool.parameters, invocation.args)
        if problems:
            return _deny(
                invocation,
                self.name,
                ErrorCode.INVALID_INPUT,
                f"Invalid arguments for '{invocation.tool.name}': {'; '.join(problems)}.",
                {"problems": problems},
            )
        if dropped:
            _record(invocation, self.name, "mutate_args", detail={"dropped": sorted(dropped)})
        return call_next(invocation.with_args(cleaned))


@dataclass(frozen=True)
class OutputGuardHook:
    """Redacts sensitive keys and caps result size. Innermost, so each attempt
    guards its own fresh output rather than re-truncating a truncated one."""

    name: str = "output_guard"

    def __call__(self, invocation: ToolInvocation, call_next: Next) -> dict:
        result = call_next(invocation)
        if not isinstance(result, dict):
            return result
        redacted, hits = _redact(result, invocation.budget.policy.redacted_keys)
        if hits:
            _record(invocation, self.name, "redact", detail={"keys": sorted(hits)})
        guarded, detail = _shrink(redacted, invocation.budget.policy.max_output_bytes)
        if detail:
            _record(invocation, self.name, "truncate", detail=detail)
        return guarded


DEFAULT_TOOL_HOOKS: tuple[ToolHook, ...] = (
    # Retry is outermost so a re-invocation re-checks the deadline and spends
    # call budget. Deadline and budget precede validation so a refused call is
    # not charged for it. Output guard is innermost: it must see raw output.
    RetryHook(),
    DeadlineHook(),
    CallBudgetHook(),
    PolicyDenyHook(),
    ApprovalHook(),
    SchemaHook(),
    OutputGuardHook(),
)


def _link(hook: ToolHook, call_next: Next) -> Next:
    def invoke(invocation: ToolInvocation) -> dict:
        return hook(invocation, call_next)

    return invoke


def _deny(invocation: ToolInvocation, hook: str, code: ErrorCode, message: str, detail: dict) -> dict:
    _record(invocation, hook, "deny", code=str(code), detail={**detail, "reason": message})
    return tool_error(code, message)


def _record(invocation: ToolInvocation, hook: str, action: str, code: str | None = None, detail: dict | None = None) -> None:
    invocation.budget.record(
        HookDecision(hook=hook, tool=invocation.tool.name, action=action, code=code, detail=detail or {})
    )


def _validate(parameters: dict, args: dict) -> tuple[dict, list[str], list[str]]:
    properties = parameters.get("properties", {})
    required = parameters.get("required", [])
    problems = [f"missing required argument '{name}'" for name in required if args.get(name) is None]
    cleaned: dict = {}
    dropped: list[str] = []
    for key, value in args.items():
        schema = properties.get(key)
        if schema is None:
            dropped.append(key)
            continue
        coerced, problem = _coerce(key, schema, value)
        if problem:
            problems.append(problem)
            continue
        cleaned[key] = coerced
    return cleaned, dropped, problems


def _coerce(key: str, schema: dict, value: object) -> tuple[object, str | None]:
    expected = schema.get("type", "string")
    enum = schema.get("enum")
    if expected == "integer":
        value, problem = _as_integer(key, value)
        if problem:
            return None, problem
    elif expected == "string":
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            return None, f"'{key}' must be a string"
        value = value if isinstance(value, str) else str(value)
    elif expected == "array":
        if not isinstance(value, list):
            value = [value]  # A single value where a list belongs is a common, safe-to-fix slip.
        item_type = (schema.get("items") or {}).get("type", "string")
        if item_type == "string":
            value = [item if isinstance(item, str) else str(item) for item in value]
    if enum and value not in enum:
        return None, f"'{key}' must be one of {', '.join(map(str, enum))}"
    return value, None


def _as_integer(key: str, value: object) -> tuple[int | None, str | None]:
    if isinstance(value, bool):
        return None, f"'{key}' must be an integer"
    if isinstance(value, int):
        return value, None
    if isinstance(value, float) and value.is_integer():
        return int(value), None
    if isinstance(value, str):
        try:
            return int(value.strip()), None
        except ValueError:
            return None, f"'{key}' must be an integer"
    return None, f"'{key}' must be an integer"


def _redact(value: object, keys: frozenset[str], hits: set[str] | None = None) -> tuple[object, set[str]]:
    """Redacts by key name only. A regex over text would mangle legitimate user documents."""
    hits = hits if hits is not None else set()
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if str(key).lower() in keys:
                result[key] = "[redacted]"
                hits.add(str(key))
            else:
                result[key], _ = _redact(item, keys, hits)
        return result, hits
    if isinstance(value, list):
        return [_redact(item, keys, hits)[0] for item in value], hits
    return value, hits


def _shrink(result: dict, limit: int) -> tuple[dict, dict | None]:
    if _size(result) <= limit:
        return result, None
    shrunk, detail = _shrink_largest_list(result, limit)
    if detail:
        return shrunk, detail
    return _shrink_largest_string(result, limit)


def _shrink_largest_list(result: dict, limit: int) -> tuple[dict, dict | None]:
    candidates = [(key, value) for key, value in result.items() if isinstance(value, list) and value]
    if not candidates:
        return result, None
    key, rows = max(candidates, key=lambda item: _size(item[1]))
    kept = len(rows)
    while kept > 0:
        kept //= 2
        candidate = {**result, key: rows[:kept], "truncated": True, "omitted_rows": len(rows) - kept}
        if _size(candidate) <= limit:
            return candidate, {"key": key, "kept": kept, "omitted_rows": len(rows) - kept}
    return {**result, key: [], "truncated": True, "omitted_rows": len(rows)}, {"key": key, "kept": 0, "omitted_rows": len(rows)}


def _shrink_largest_string(result: dict, limit: int) -> tuple[dict, dict | None]:
    candidates = [(key, value) for key, value in result.items() if isinstance(value, str) and value]
    if not candidates:
        return result, None
    key, text = max(candidates, key=lambda item: len(item[1]))
    overflow = _size(result) - limit
    kept = max(0, len(text) - overflow - 64)
    return (
        {**result, key: text[:kept], "truncated": True, "omitted_characters": len(text) - kept},
        {"key": key, "kept": kept, "omitted_characters": len(text) - kept},
    )


def _size(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))
