from dataclasses import dataclass
from fastapi import HTTPException
from openai import AuthenticationError, APIConnectionError, APIStatusError


@dataclass
class ApiError(Exception):
    code: str
    message: str
    action: str
    status_code: int = 400

    def as_payload(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "action": self.action}


def normalize_error(error: Exception) -> ApiError:
    if isinstance(error, ApiError):
        return error
    if isinstance(error, AuthenticationError):
        return ApiError("LLM_AUTH_FAILED", "The configured LLM rejected authentication.", "Set a valid LLM_API_KEY, then recreate api and worker containers.", 502)
    if isinstance(error, APIConnectionError):
        return ApiError("LLM_UNREACHABLE", "The configured LLM endpoint could not be reached.", "Check LLM_BASE_URL and outbound network access, then retry.", 502)
    if isinstance(error, APIStatusError):
        if error.status_code == 402:
            return ApiError("LLM_INSUFFICIENT_BALANCE", "The configured LLM provider reports an insufficient balance.", "Top up the provider account, then retry the question.", 502)
        return ApiError("LLM_REQUEST_FAILED", f"The configured LLM rejected the request ({error.status_code}).", "Check LLM_MODEL and provider account status, then inspect api logs.", 502)
    if isinstance(error, HTTPException):
        return ApiError("REQUEST_FAILED", str(error.detail), "Correct the request and retry.", error.status_code)
    return ApiError("INTERNAL_ERROR", "The analysis request failed unexpectedly.", "Inspect `docker compose logs api` using the request timestamp.", 500)
