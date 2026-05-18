#!/usr/bin/env bash
# pull_models.sh — Pulls all required Ollama models after the service starts.
# Called by docker-compose as the Ollama entrypoint so models are always present.

set -euo pipefail

OLLAMA_HOST="${OLLAMA_HOST:-http://localhost:11434}"

# Models required by the application
MODELS=(
  "${SLM_MODEL_NAME:-phi3:mini}"
  "${LLM_MODEL_NAME:-llama3}"
  "${EMBEDDING_MODEL:-nomic-embed-text}"
)

echo "==> Waiting for Ollama to be ready at $OLLAMA_HOST …"
for i in $(seq 1 30); do
  if curl -sf "$OLLAMA_HOST/api/tags" > /dev/null 2>&1; then
    echo "==> Ollama is up."
    break
  fi
  echo "    Attempt $i/30 — not ready yet, retrying in 3s …"
  sleep 3
done

for model in "${MODELS[@]}"; do
  echo "==> Pulling model: $model"
  if curl -sf "$OLLAMA_HOST/api/tags" | grep -q "\"$model\""; then
    echo "    Already present, skipping."
  else
    curl -sf -X POST "$OLLAMA_HOST/api/pull" \
      -H "Content-Type: application/json" \
      -d "{\"name\": \"$model\"}" | grep -v '^$' || true
    echo "    Done."
  fi
done

echo "==> All models ready."
