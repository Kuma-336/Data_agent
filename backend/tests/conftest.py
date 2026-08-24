import os
from pathlib import Path

os.environ.setdefault("APP_SECRET_KEY", "test-app-secret-with-at-least-thirty-two-bytes")
os.environ.setdefault("FIELD_ENCRYPTION_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=")
os.environ.setdefault("LLM_API_KEY", "test-key")
os.environ.setdefault("LLM_BASE_URL", "https://example.test")
os.environ.setdefault("LLM_MODEL", "test-model")
os.environ.setdefault("DATABASE_URL", "sqlite:///./test.db")
os.environ.setdefault("DATA_DIR", str(Path("./test-data").resolve()))
