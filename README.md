# Personal Data Agent

A small, self-hosted data-analysis agent. It accepts CSV/XLSX files and user-owned PostgreSQL read-only connections, then lets an OpenAI-compatible model inspect data, issue guarded SQL, and return auditable answers.

## Run

1. Copy `.env.example` to `.env` and set `APP_SECRET_KEY`, `FIELD_ENCRYPTION_KEY`, `LLM_API_KEY`, `LLM_BASE_URL`, and `LLM_MODEL`.
2. Run `docker compose up --build`.
3. Open `http://localhost:5173`.

If Docker lacks the Compose plugin, run `powershell -ExecutionPolicy Bypass -File scripts/start.ps1` from this directory instead.

The application database is the Compose PostgreSQL service. Any business PostgreSQL connection is configured per user and must use a read-only database account.

## Safety model

- Business SQL accepts one `SELECT` or `WITH` statement only and runs in a read-only transaction.
- Query output is capped and stored with a user-scoped query id for later display.
- Data-source passwords are encrypted with the Fernet key in `FIELD_ENCRYPTION_KEY`.
- Uploaded files and all API resources are scoped to the authenticated user.

This project includes nao's `example/jaffle_shop.duckdb` as a mounted, read-only sample source. In the attachment menu, choose one or more **jaffle_shop tables**; the agent receives only their schemas and can query only the selected table names.

This project is inspired by nao's Apache-2.0-licensed tool-schema and agent-loop design, but is a Python implementation with a narrower scope.

## Connect Codex or another MCP client

data agent can expose its governed analytics capabilities over a Streamable HTTP MCP endpoint, mirroring nao's three modes: full-agent questions, context/guarded SQL, and reports. It is disabled by default. Set `MCP_ENABLED=true` in `.env`, restart the API, sign in to data agent, then call `POST /mcp/tokens` once with your session cookie to obtain a 90-day bearer token.

Add a Streamable HTTP server in Codex with URL `http://localhost:8000/mcp` and header `Authorization: Bearer <token>`. The exposed tools are `ask_data_agent`, `execute_sql`, `grep`, `ls`, and report create/read/update/archive tools. SQL remains restricted to one read-only `SELECT`/`WITH` statement and every MCP request stays scoped to the token owner's files, connections, context, chats, and reports. Permanent report deletion is intentionally not exposed over MCP.

## Verification

All release checks use a real DeepSeek request. Set a dedicated billable test key, then run:

```powershell
$env:DATA_AGENT_TEST_LLM_API_KEY = 'your-test-key'
powershell -ExecutionPolicy Bypass -File scripts/test-stack.ps1
```

For a failed runtime request, inspect `docker compose logs -f api`. Configuration errors prevent the API from starting; model authentication errors are returned to the UI as `LLM_AUTH_FAILED` with a remediation action.

## Reproducible agent benchmark

The versioned Northwind benchmark contains 150 deterministic reference-SQL cases plus a lock file. Validate the committed fixture with:

```powershell
python scripts/generate_northwind_eval_cases.py --check
```

Prepare the pinned, read-only PostgreSQL dataset with:

```powershell
python scripts/bootstrap_northwind_eval.py --download --start
```

See `docs/agent-evolution/evaluation-plan.md` for the import API, scoring contract, smoke/full run order, and comparison metrics.
