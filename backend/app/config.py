from pathlib import Path
from cryptography.fernet import Fernet
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_secret_key: str
    field_encryption_key: str
    database_url: str = "postgresql+psycopg://data_agent:data_agent@localhost:5432/data_agent"
    llm_base_url: str = "https://api.deepseek.com"
    llm_api_key: str = ""
    llm_model: str = "deepseek-chat"
    allowed_db_hosts: str = "localhost"
    max_query_rows: int = 1000
    max_upload_mb: int = 25
    data_dir: Path = Path("/data")
    # Set this in a deployed container to a read-only mounted copy of the sample DB.
    jaffle_shop_database_path: Path | None = None
    redis_url: str = "redis://redis:6379/0"
    llm_context_window: int = 64_000
    llm_max_output_tokens: int = 4_096
    # Persisted schema/profiling/rules context files, read on demand per database.
    context_max_tables: int = 200
    context_preview_rows: int = 10
    context_profile_sample_rows: int = 50_000
    context_profile_top_values: int = 10
    context_profile_max_distinct: int = 50
    context_max_rules_chars: int = 50_000
    # External agents can read user-owned data through MCP; opt in per deployment.
    mcp_enabled: bool = False
    mcp_subagent_enabled: bool = True
    mcp_context_enabled: bool = True
    mcp_reports_enabled: bool = True
    mcp_token_ttl_days: int = 90

    @model_validator(mode="after")
    def validate_runtime_configuration(self) -> "Settings":
        placeholders = {"", "replace-with-a-long-random-secret", "replace-with-a-fernet-key"}
        if self.app_secret_key in placeholders:
            raise ValueError("APP_SECRET_KEY must be set to a non-placeholder secret")
        if self.field_encryption_key in placeholders:
            raise ValueError("FIELD_ENCRYPTION_KEY must be a valid Fernet key, not the example placeholder")
        try:
            Fernet(self.field_encryption_key.encode())
        except Exception as exc:
            raise ValueError("FIELD_ENCRYPTION_KEY must be 32 url-safe base64-encoded bytes") from exc
        if not self.llm_api_key.strip():
            raise ValueError("LLM_API_KEY is required; set a valid provider key before starting the API")
        if not self.llm_base_url.strip() or not self.llm_model.strip():
            raise ValueError("LLM_BASE_URL and LLM_MODEL are required")
        return self

    @property
    def allowed_hosts(self) -> set[str]:
        return {host.strip().lower() for host in self.allowed_db_hosts.split(",") if host.strip()}


settings = Settings()
