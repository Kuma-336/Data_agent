param([switch]$KeepStack)
$ErrorActionPreference = 'Stop'
if (-not $env:DATA_AGENT_TEST_LLM_API_KEY) { throw 'Set DATA_AGENT_TEST_LLM_API_KEY to a dedicated DeepSeek test key. This test makes a real billable LLM request.' }

$testEnv = Join-Path $PSScriptRoot '..\.env.test'
$csv = Join-Path $env:TEMP 'data-agent-smoke.csv'
$cookie = Join-Path $env:TEMP 'data-agent-smoke.cookies'
@"
APP_SECRET_KEY=data-agent-test-secret-with-at-least-thirty-two-bytes
FIELD_ENCRYPTION_KEY=MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=
LLM_BASE_URL=https://api.deepseek.com
LLM_API_KEY=$($env:DATA_AGENT_TEST_LLM_API_KEY)
LLM_MODEL=deepseek-chat
ALLOWED_DB_HOSTS=localhost
MAX_QUERY_ROWS=1000
MAX_UPLOAD_MB=25
"@ | Set-Content -LiteralPath $testEnv -NoNewline

$compose = @('-p', 'data-agent-test', '-f', 'docker-compose.yml', '-f', 'docker-compose.test.yml')
try {
  docker compose @compose --env-file .env.test config --quiet
  docker build --target test -t data-agent-backend-tests backend
  docker run --rm -e APP_SECRET_KEY=test-app-secret-with-at-least-thirty-two-bytes -e FIELD_ENCRYPTION_KEY=MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY= -e LLM_API_KEY=test-key -e LLM_BASE_URL=https://example.test -e LLM_MODEL=test-model -e DATABASE_URL=sqlite:///./test.db data-agent-backend-tests
  docker compose @compose --env-file .env.test up --build --wait
  $email = "smoke-$([guid]::NewGuid().ToString('N'))@example.test"
  $register = curl.exe -fsS -c $cookie -H 'Content-Type: application/json' -d "{\"email\":\"$email\",\"password\":\"a-long-test-password\"}" http://localhost:18000/auth/register
  if (-not ($register | ConvertFrom-Json).id) { throw 'Registration smoke test failed' }
  "product,amount`nA,10`nB,25" | Set-Content -LiteralPath $csv
  $file = curl.exe -fsS -b $cookie -F "upload=@$csv;type=text/csv" http://localhost:18000/files | ConvertFrom-Json
  $chat = curl.exe -fsS -b $cookie -H 'Content-Type: application/json' -d '{"title":"smoke"}' http://localhost:18000/chats | ConvertFrom-Json
  $body = "{\"content\":\"Inspect the attached CSV, then use execute_sql on target data to calculate total amount.\",\"file_ids\":[\"$($file.id)\"],\"connection_ids\":[]}"
  $stream = curl.exe -fsSN -b $cookie -H 'Content-Type: application/json' -d $body "http://localhost:18000/chats/$($chat.id)/messages"
  if ($stream -notmatch 'event: tool' -or $stream -notmatch 'execute_sql' -or $stream -notmatch 'event: answer') { throw "Real LLM smoke test failed: $stream" }
  Write-Host 'PASS: unit, Compose, auth/upload, and real DeepSeek analysis smoke tests.'
} finally {
  if (-not $KeepStack) { docker compose @compose --env-file .env.test down -v --remove-orphans }
  Remove-Item -LiteralPath $testEnv,$csv,$cookie -Force -ErrorAction SilentlyContinue
}
