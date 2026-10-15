# Fallback for Docker installations without the `docker compose` plugin.
# Requires .env at the project root; create it from .env.example first.
if (-not (Test-Path .env)) { throw 'Create .env from .env.example and set all required secrets first.' }

docker network inspect data-agent-network *> $null
if ($LASTEXITCODE -ne 0) { docker network create data-agent-network }
docker volume inspect data-agent-db *> $null
if ($LASTEXITCODE -ne 0) { docker volume create data-agent-db }
docker volume inspect data-agent-files *> $null
if ($LASTEXITCODE -ne 0) { docker volume create data-agent-files }

if (-not (docker ps -aq -f name=^/data-agent-db$)) {
  docker run -d --name data-agent-db --restart unless-stopped --network data-agent-network -e POSTGRES_USER=data_agent -e POSTGRES_PASSWORD=data_agent -e POSTGRES_DB=data_agent -v data-agent-db:/var/lib/postgresql/data postgres:16-alpine
}
if (-not (docker ps -aq -f name=^/data-agent-redis$)) { docker run -d --name data-agent-redis --restart unless-stopped --network data-agent-network redis:7-alpine }

docker build -t personal-data-agent-api backend
docker build -t personal-data-agent-web frontend
if (-not (docker ps -aq -f name=^/data-agent-api$)) {
  docker run -d --name data-agent-api --restart unless-stopped --network data-agent-network --env-file .env -e DATABASE_URL=postgresql+psycopg://data_agent:data_agent@data-agent-db:5432/data_agent -e REDIS_URL=redis://data-agent-redis:6379/0 -e DATA_DIR=/data -e JAFFLE_SHOP_DATABASE_PATH=/seed/jaffle_shop.duckdb -v data-agent-files:/data -v "${PWD}\..\example\jaffle_shop.duckdb:/seed/jaffle_shop.duckdb:ro" -p 8000:8000 personal-data-agent-api
}
if (-not (docker ps -aq -f name=^/data-agent-worker$)) {
  docker run -d --name data-agent-worker --restart unless-stopped --network data-agent-network --env-file .env -e DATABASE_URL=postgresql+psycopg://data_agent:data_agent@data-agent-db:5432/data_agent -e REDIS_URL=redis://data-agent-redis:6379/0 personal-data-agent-api dramatiq app.worker
}
if (-not (docker ps -aq -f name=^/data-agent-web$)) { docker run -d --name data-agent-web --restart unless-stopped -p 5173:80 personal-data-agent-web }
docker ps --filter name=data-agent --format '{{.Names}} {{.Status}} {{.Ports}}'
