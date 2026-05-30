#!/usr/bin/env bash
# pull_models.sh — Pulls all required Ollama models after the service starts.
# Called by docker-compose as the Ollama entrypoint so models are always present.

set -euo pipefail

OLLAMA_HOST="${OLLAMA_HOST:-http://localhost:11434}"

# Models required by the application
MODELS=(
  "${SLM_MODEL_NAME:-qwen2:0.5b}"
  "${LLM_MODEL_NAME:-qwen2:1.5b}"
  "${EMBEDDING_MODEL:-nomic-embed-text}"
  "${VISION_MODEL:-llava:7b}"
)

echo "==> Waiting for Ollama to be ready…"
for i in $(seq 1 30); do
  if ollama list > /dev/null 2>&1; then
    echo "==> Ollama is up."
    break
  fi
  echo "    Attempt $i/30 — not ready yet, retrying in 3s …"
  sleep 3
done

for model in "${MODELS[@]}"; do
  echo "==> Pulling model: $model"
  ollama pull "$model" || true
done

echo "==> All models ready."
