# LLM Providers for Generation and Marking

## Current Provider Setup

All LLM tasks (question generation, answer marking, vision, math extraction) use **paid cloud APIs**. There are no local models. The system automatically rotates between providers when one hits its quota.

| Task | Primary | Fallback |
|---|---|---|
| Embeddings | Gemini `gemini-embedding-001` (free) | OpenAI `text-embedding-3-small` (paid) |
| Vision / charts | OpenAI `gpt-4o-mini` | Anthropic `claude-haiku-4-5-20251001` |
| Math extraction | OpenAI `gpt-4o-mini` | Anthropic `claude-haiku-4-5-20251001` |
| Question generation | OpenAI `gpt-4o-mini` | Anthropic `claude-haiku-4-5-20251001` → Gemini |
| Answer marking | OpenAI `gpt-4o-mini` | Anthropic `claude-haiku-4-5-20251001` → Gemini |

---

## Why These Providers

**Gemini (embeddings)** — The MongoDB vector index is built on 768-dim Gemini embeddings. Keeping the same model for new embeddings ensures search results are consistent. OpenAI `text-embedding-3-small` with `dimensions=768` is used as fallback and produces the same dimension, so the index works with either.

**OpenAI gpt-4o-mini (primary)** — Chosen for its balance of cost (~$0.15/1M input tokens), speed, and quality. Vision capability (chart descriptions, math extraction) is built-in. Rate limits are high enough for concurrent ingestion (500 RPM).

**Anthropic claude-haiku (fallback)** — Fast, cheap ($0.80/1M input), and reliable. Activates automatically when OpenAI hits a rate limit or daily quota. No configuration needed.

---

## API Key Rotation

`api_key_manager.py` tracks the health of each provider. When a 429 or quota error is received:

- **Rate limit (429)**: provider enters 60-second cooldown, next provider used
- **Quota exhaustion**: provider enters 1-hour cooldown, next provider used
- **Automatic recovery**: after cooldown expires, provider is tried again

To manually reset cooldowns (e.g. after adding quota or new keys):
```bash
curl -X POST http://localhost:8000/api/v1/admin/api-status/reset-cooldowns \
  -H "Authorization: Bearer <token>"
```

---

## Estimated Cost

For a 600-page statistics textbook (585 chunks, 275 chart pages, 345 math pages):

| Step | Provider | Cost |
|---|---|---|
| Chart descriptions (275 pages) | OpenAI gpt-4o-mini vision | ~$0.20 |
| Math extraction (345 pages) | OpenAI gpt-4o-mini vision | ~$0.10 |
| Embeddings (585 chunks) | OpenAI text-embedding-3-small | ~$0.005 |
| **Total ingestion** | | **~$0.31** |
| Question generation (13 chapters × 10 questions) | OpenAI gpt-4o-mini | ~$0.05 |
| Per student submission marking | OpenAI gpt-4o-mini | ~$0.002 |

With $5 in OpenAI and $5 in Anthropic, you can ingest many books and mark thousands of submissions before needing to top up.

---

## Changing Models

Update `.env` to use a different model:

```env
# Use GPT-4o instead of gpt-4o-mini for higher quality generation
OPENAI_GENERATION_MODEL=gpt-4o
GENERATION_LLM_MODEL=gpt-4o

# Use Claude Sonnet instead of Haiku for better fallback quality
ANTHROPIC_GENERATION_MODEL=claude-sonnet-4-6
ANTHROPIC_MARKING_MODEL=claude-sonnet-4-6
```

Then restart: `docker compose restart worker-gen worker-mark backend`
