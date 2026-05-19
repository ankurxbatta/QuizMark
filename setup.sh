#!/usr/bin/env bash
# setup.sh – First-run initialisation (Linux / macOS)
set -e

echo "==> Checking for .env..."
if [ ! -f .env ]; then
  cp .env.example .env
  echo ""
  echo "  ✅  .env created from .env.example"
  echo "  ⚠️   Edit .env and set SECRET_KEY, POSTGRES_PASSWORD, and ADMIN_PASSWORD before continuing."
  echo "  Then run ./setup.sh again."
  echo ""
  exit 1
fi

echo "==> Pulling Docker images..."
docker compose pull --quiet

echo "==> Building application images..."
docker compose build --quiet

echo "==> Starting database and broker..."
docker compose up -d db broker

echo "==> Waiting for Postgres to be healthy..."
until docker compose exec db pg_isready \
  -U "$(grep ^POSTGRES_USER .env | cut -d= -f2)" \
  -d "$(grep ^POSTGRES_DB   .env | cut -d= -f2)" > /dev/null 2>&1; do
  printf '.'
  sleep 2
done
echo " ready."

echo "==> Running Alembic migrations..."
docker compose run --rm --no-deps backend alembic upgrade head

echo "==> Starting Ollama service..."
docker compose up -d llm
sleep 5

echo "==> Pulling LLM models (this may take several minutes)..."
LLM_MODEL=$(grep ^LLM_MODEL_NAME  .env | cut -d= -f2 || echo "qwen2:1.5b")
SLM_MODEL=$(grep ^SLM_MODEL_NAME  .env | cut -d= -f2 || echo "qwen2:0.5b")
EMBED_MODEL=$(grep ^EMBEDDING_MODEL .env | cut -d= -f2 || echo "nomic-embed-text")

docker compose exec llm ollama pull "${LLM_MODEL}"
docker compose exec llm ollama pull "${SLM_MODEL}"
docker compose exec llm ollama pull "${EMBED_MODEL}"

echo "==> Starting all services..."
docker compose up -d

echo ""
echo "✅  Setup complete!"
echo ""
echo "   Frontend   →  http://localhost:3000"
echo "   API docs   →  http://localhost:8000/docs"
echo "   Ollama     →  http://localhost:11434"
echo ""
echo "   (Optional) Seed the Q&A bank:"
echo "   python3 scripts/generate_data.py"
echo ""
echo "   Models running:"
echo "     Tier-1 SLM   : ${SLM_MODEL}   (fast pre-scorer)"
echo "     Tier-3 LLM   : ${LLM_MODEL}   (offline marker)"
echo "     Embeddings   : ${EMBED_MODEL}"
