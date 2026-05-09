#!/usr/bin/env bash
# setup.sh – First-run initialisation for Linux/macOS
set -e

echo "==> Copying environment template..."
if [ ! -f .env ]; then
  cp .env.example .env
  echo "    .env created – please review and set your secrets before continuing."
  exit 1
fi

echo "==> Pulling Docker images..."
docker compose pull

echo "==> Building application images..."
docker compose build

echo "==> Starting services (db + broker first)..."
docker compose up -d db broker

echo "==> Waiting for database to be ready..."
until docker compose exec db pg_isready -U "$(grep POSTGRES_USER .env | cut -d= -f2)" > /dev/null 2>&1; do
  sleep 2
done

echo "==> Running Alembic database migrations..."
docker compose run --rm backend alembic upgrade head

echo "==> Pulling LLM models via Ollama..."
LLM_MODEL=$(grep LLM_MODEL_NAME .env | cut -d= -f2)
EMBED_MODEL=$(grep EMBEDDING_MODEL .env | cut -d= -f2)
docker compose run --rm llm ollama pull "${LLM_MODEL:-llama3}"
docker compose run --rm llm ollama pull "${EMBED_MODEL:-nomic-embed-text}"

echo "==> Starting all services..."
docker compose up -d

echo ""
echo "✅  Setup complete!"
echo "   Frontend : http://localhost:3000"
echo "   Backend  : http://localhost:8000/docs"
echo "   Ollama   : http://localhost:11434"
