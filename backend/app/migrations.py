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
    ]
    with engine.begin() as connection:
        for statement in statements:
            try:
                connection.execute(text(statement))
            except Exception:
                # SQLite's existing local development databases may not support IF NOT EXISTS;
                # Base.metadata.create_all below still creates a clean test database.
                pass
