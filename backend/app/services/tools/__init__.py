"""The agent's tool set and its governance chain.

Importing this package installs the hook chain on the module registry, then
registers every tool. Module import order sets the order the definitions are
advertised to the model, so keep inspection before querying.
"""

from .registry import Tool, ToolContext, ToolRegistry, registry
from .hooks import DEFAULT_TOOL_HOOKS, ToolInvocation, compose
from .policy import Budget, HookDecision, ToolPolicy, invocation_fingerprint, resolve_policy

registry.install_hooks(DEFAULT_TOOL_HOOKS)

from . import inspection, table_context, sql, interaction, artifacts, documents, reporting  # noqa: E402,F401

__all__ = [
    "Budget",
    "DEFAULT_TOOL_HOOKS",
    "HookDecision",
    "Tool",
    "ToolContext",
    "ToolInvocation",
    "ToolPolicy",
    "ToolRegistry",
    "compose",
    "invocation_fingerprint",
    "registry",
    "resolve_policy",
]
