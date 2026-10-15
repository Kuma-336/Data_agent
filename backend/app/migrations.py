from sqlalchemy import text
from .db import engine


def upgrade_schema() -> None:
    """Additive migration for existing local deployments before Alembic is introduced."""
    statements = [
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS memory_enabled BOOLEAN DEFAULT TRUE",
        "ALTER TABLE chats ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP NULL",
        "ALTER TABLE messages ADD COLUMN IF NOT EXISTS superseded_at TIMESTAMP NULL",
        "ALTER TABLE messages ADD COLUMN IF NOT EXISTS version_group_id VARCHAR(36) NULL",
        "ALTER TABLE memories ADD COLUMN IF NOT EXISTS superseded_by VARCHAR(36) NULL",
        "ALTER TABLE memories ADD COLUMN IF NOT EXISTS source_message_id VARCHAR(36) NULL",
        "ALTER TABLE memories ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP NULL",
        "ALTER TABLE reports ADD COLUMN IF NOT EXISTS archived_at TIMESTAMP NULL",
        "ALTER TABLE eval_suites ADD COLUMN IF NOT EXISTS fixture_hash VARCHAR(64) NULL",
        "ALTER TABLE eval_suites ADD COLUMN IF NOT EXISTS dataset JSON NULL",
        "ALTER TABLE eval_cases ADD COLUMN IF NOT EXISTS case_key VARCHAR(80) NULL",
        "ALTER TABLE eval_cases ADD COLUMN IF NOT EXISTS category VARCHAR(80) NULL",
        "ALTER TABLE eval_cases ADD COLUMN IF NOT EXISTS difficulty VARCHAR(24) NULL",
        "ALTER TABLE eval_cases ADD COLUMN IF NOT EXISTS acceptance JSON NULL",
        "ALTER TABLE data_sources ADD COLUMN IF NOT EXISTS context_synced_at TIMESTAMP NULL",
    ]
    with engine.begin() as connection:
        for statement in statements:
            try:
                connection.execute(text(statement))
            except Exception:
                # SQLite's existing local development databases may not support IF NOT EXISTS;
                # Base.metadata.create_all below still creates a clean test database.
                pass
