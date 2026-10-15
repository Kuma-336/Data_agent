"""Persisted per-database context files: schema, profiling, and business rules.

Layout under ``settings.data_dir``::

    context/user=<user_id>/<kind>=<asset_id>/RULES.md
    context/user=<user_id>/<kind>=<asset_id>/tables/<table>/columns.md

A :class:`ContextTarget` can only be built from a record the caller already
loaded for the authenticated user, so read access always follows ownership of
the underlying database.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from fastapi import HTTPException

from ..config import settings
from ..models import DataSource, UploadedFile

DOCUMENT_KINDS = ("columns", "profiling", "preview")
SAMPLE_ASSET_ID = "jaffle_shop"
RULES_FILENAME = "RULES.md"

_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]{0,119}$")


@dataclass(frozen=True)
class ContextTarget:
    """An authorized handle to one database's context directory."""

    kind: str
    asset_id: str
    user_id: str
    label: str

    @property
    def reference(self) -> str:
        return self.asset_id if self.kind == "connection" else f"{self.kind}:{self.asset_id}"


def connection_target(source: DataSource) -> ContextTarget:
    return ContextTarget("connection", source.id, source.user_id, source.name)


def file_target(file: UploadedFile) -> ContextTarget:
    return ContextTarget("file", file.id, file.user_id, file.original_name)


def sample_target(user_id: str) -> ContextTarget:
    return ContextTarget("sample", SAMPLE_ASSET_ID, user_id, "jaffle_shop sample")


def read_rules(target: ContextTarget) -> str | None:
    return _read_text(_target_root(target) / RULES_FILENAME)


def write_rules(target: ContextTarget, content: str) -> Path:
    path = _target_root(target) / RULES_FILENAME
    return _write_text(path, content[: settings.context_max_rules_chars])


def read_table_document(target: ContextTarget, table: str, kind: str) -> str | None:
    return _read_text(_document_path(target, table, kind))


def write_table_document(target: ContextTarget, table: str, kind: str, content: str) -> Path:
    return _write_text(_document_path(target, table, kind), content)


def list_tables(target: ContextTarget) -> list[dict]:
    """Table names plus which documents are present, for prompt indexes."""
    tables_root = _target_root(target) / "tables"
    if not tables_root.is_dir():
        return []
    entries = []
    for directory in sorted(path for path in tables_root.iterdir() if path.is_dir()):
        documents = [kind for kind in DOCUMENT_KINDS if (directory / f"{kind}.md").is_file()]
        if documents:
            entries.append({"table": directory.name, "documents": documents})
    return entries


def clear_tables(target: ContextTarget) -> None:
    """Drop synced table documents so a re-sync cannot leave stale tables behind."""
    shutil.rmtree(_target_root(target) / "tables", ignore_errors=True)


def _document_path(target: ContextTarget, table: str, kind: str) -> Path:
    if kind not in DOCUMENT_KINDS:
        raise HTTPException(400, f"Unknown context document kind '{kind}'; expected one of {', '.join(DOCUMENT_KINDS)}")
    return _target_root(target) / "tables" / _safe_segment(table, "table name") / f"{kind}.md"


def _target_root(target: ContextTarget) -> Path:
    root = (
        settings.data_dir
        / "context"
        / f"user={_safe_segment(target.user_id, 'user id')}"
        / f"{_safe_segment(target.kind, 'target kind')}={_safe_segment(target.asset_id, 'asset id')}"
    )
    return _assert_within(settings.data_dir / "context", root)


def _safe_segment(value: str, label: str) -> str:
    if not _SAFE_SEGMENT.match(value or ""):
        raise HTTPException(400, f"Invalid {label}: only letters, digits, dot, dash and underscore are allowed")
    return value


def _assert_within(root: Path, candidate: Path) -> Path:
    resolved_root = root.resolve()
    resolved = Path(candidate).resolve()
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise HTTPException(400, "Context path escapes the context directory")
    return resolved


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def _write_text(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path
