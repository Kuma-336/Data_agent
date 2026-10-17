"""Error taxonomy for tool results.

Two sets, two consumers: `MODEL_MAY_RETRY` decides whether the model is told it
may try again, `AUTO_RETRY` decides what the retry hook silently re-invokes.
They differ on `invalid_input` on purpose — the model should retry with corrected
arguments, but re-sending the same arguments would fail identically.
"""

from __future__ import annotations

from enum import StrEnum

from fastapi import HTTPException


class ErrorCode(StrEnum):
    RETRYABLE = "retryable"
    PERMISSION_DENIED = "permission_denied"
    INVALID_INPUT = "invalid_input"
    RESOURCE_UNAVAILABLE = "resource_unavailable"
    POLICY_VIOLATION = "policy_violation"
    INTERNAL_ERROR = "internal_error"
    # CLARIFICATION_REQUIRED is intentionally absent: the `clarify` tool is that path.


MODEL_MAY_RETRY = frozenset({ErrorCode.RETRYABLE, ErrorCode.RESOURCE_UNAVAILABLE, ErrorCode.INVALID_INPUT})
AUTO_RETRY = frozenset({ErrorCode.RETRYABLE, ErrorCode.RESOURCE_UNAVAILABLE})

_STATUS_CODES = {
    400: ErrorCode.INVALID_INPUT,
    403: ErrorCode.PERMISSION_DENIED,
    404: ErrorCode.RESOURCE_UNAVAILABLE,
    413: ErrorCode.INVALID_INPUT,
}


def tool_error(code: ErrorCode, message: str, **extra) -> dict:
    payload = {"error": message, "code": str(code), **extra}
    if code in MODEL_MAY_RETRY:
        payload["retryable"] = True
    return payload


def error_from_http_exception(exc: HTTPException) -> dict:
    fallback = ErrorCode.RETRYABLE if exc.status_code >= 500 else ErrorCode.INVALID_INPUT
    return tool_error(_STATUS_CODES.get(exc.status_code, fallback), str(exc.detail))


def error_code(result: object) -> ErrorCode | None:
    """The code carried by a failed tool result, or None if it did not fail."""
    if not isinstance(result, dict) or not result.get("error"):
        return None
    try:
        return ErrorCode(result.get("code", ErrorCode.RETRYABLE))
    except ValueError:
        return ErrorCode.RETRYABLE
