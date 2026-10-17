"""Tool-call policy and per-turn budget.

Policy is data, not an engine: a frozen table of thresholds that built-in hooks
read. Budget is the mutable per-turn counter set, carried on ToolContext because
that is the only object spanning a turn that also reaches the registry.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from time import perf_counter

from sqlalchemy.orm import Session

from ...config import settings
from ...models import User


@dataclass(frozen=True)
class ToolPolicy:
    max_tool_calls: int
    max_iterations: int
    turn_seconds: int
    max_output_bytes: int
    max_auto_retries: int
    denied_tools: frozenset[str]
    redacted_keys: frozenset[str]
    approval_required: frozenset[str]

    @classmethod
    def from_settings(cls) -> "ToolPolicy":
        return cls(
            max_tool_calls=settings.tool_max_calls_per_turn,
            max_iterations=settings.tool_max_iterations,
            turn_seconds=settings.tool_turn_seconds,
            max_output_bytes=settings.tool_max_output_bytes,
            max_auto_retries=settings.tool_max_auto_retries,
            denied_tools=settings.denied_tools,
            redacted_keys=settings.redacted_keys,
            approval_required=settings.approval_required_tools,
        )

    def as_dict(self) -> dict:
        return {
            "max_tool_calls": self.max_tool_calls,
            "max_iterations": self.max_iterations,
            "turn_seconds": self.turn_seconds,
            "max_output_bytes": self.max_output_bytes,
            "max_auto_retries": self.max_auto_retries,
            "denied_tools": sorted(self.denied_tools),
            "approval_required": sorted(self.approval_required),
        }


@dataclass(frozen=True)
class HookDecision:
    """A hook action that changed the outcome. Allowed calls are never recorded."""

    hook: str
    tool: str
    action: str
    code: str | None = None
    detail: dict = field(default_factory=dict)


@dataclass
class Budget:
    policy: ToolPolicy = field(default_factory=ToolPolicy.from_settings)
    tool_calls: int = 0
    deadline: float = 0.0
    depth: int = 0
    approvals: frozenset[str] = frozenset()
    trail: list[HookDecision] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.deadline:
            self.deadline = perf_counter() + self.policy.turn_seconds

    @property
    def seconds_left(self) -> float:
        return self.deadline - perf_counter()

    def record(self, decision: HookDecision) -> None:
        self.trail.append(decision)

    def drain(self) -> list[HookDecision]:
        decisions, self.trail = self.trail, []
        return decisions


def resolve_policy(db: Session | None = None, user: User | None = None) -> ToolPolicy:
    """The seam for per-tenant policy; both arguments are ignored for now."""
    return ToolPolicy.from_settings()


def invocation_fingerprint(tool_name: str, args: dict) -> str:
    """Identifies one specific call, so approval cannot be reused for other arguments."""
    canonical = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(f"{tool_name}\n{canonical}".encode()).hexdigest()
