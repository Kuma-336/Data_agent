# Personal Data Agent Documentation

## Overview

Personal Data Agent is a self-hosted application for answering questions about user-owned CSV, XLSX, and PostgreSQL data. It combines a React UI, a FastAPI service, a PostgreSQL application database, a Redis-backed worker, and an OpenAI-compatible model API.

## Start locally

1. Copy `.env.example` to `.env`.
2. Set `APP_SECRET_KEY`, `FIELD_ENCRYPTION_KEY`, and `LLM_API_KEY` to secure values.
3. Run `docker compose up --build`.
4. Open `http://localhost:5173`.

The API runs at `http://localhost:8000`; use `/health` and `/ready` for health checks. If the Compose plugin is unavailable, run `powershell -ExecutionPolicy Bypass -File scripts/start.ps1`.

## Configuration

| Variable | Purpose | Default |
| --- | --- | --- |
| `APP_SECRET_KEY` | Signs authentication data. | Required |
| `FIELD_ENCRYPTION_KEY` | Fernet key for encrypted connection passwords. | Required |
| `LLM_BASE_URL` | OpenAI-compatible model endpoint. | `https://api.deepseek.com` |
| `LLM_API_KEY` | Model-provider API key. | Required |
| `LLM_MODEL` | Model identifier. | `deepseek-chat` |
| `ALLOWED_DB_HOSTS` | Allowed PostgreSQL connection hosts. | `host.docker.internal,localhost` |
| `MAX_QUERY_ROWS` | Maximum query result rows. | `1000` |
| `MAX_UPLOAD_MB` | Maximum upload size in MB. | `25` |

Do not commit `.env` or `.env.test`; Git ignores both files.

## Data safety

- Use a database account with read-only privileges for every business data source.
- The application permits only one `SELECT` or `WITH` statement and uses a read-only transaction.
- Results are limited by `MAX_QUERY_ROWS` and query records are user-scoped for auditing.
- Uploaded files, connections, conversations, and passwords are user-scoped; passwords are encrypted with `FIELD_ENCRYPTION_KEY`.

## Verification

The full-stack script performs backend tests, starts an isolated stack, tests authentication and file upload, and makes a real billable model request. Set a dedicated key before running it:

```powershell
$env:DATA_AGENT_TEST_LLM_API_KEY = 'your-dedicated-test-key'
powershell -ExecutionPolicy Bypass -File scripts/test-stack.ps1
```

For diagnostics, use `docker compose logs -f api`.

## Project layout

```text
backend/   FastAPI service, agent logic, SQL guard, persistence, and tests
frontend/  Vite + React client and Playwright tests
scripts/   Start and full-stack verification scripts
```
