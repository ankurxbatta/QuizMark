# Choosing and setting up an LLM for generation

The system needs an LLM to actually generate the questions. By default it's set up to use an online provider — I've tested it with Claude, GPT-4o, and Gemini. There's also a local fallback using qwen2:0.5b through Ollama if you don't want to use an external API.

---

## Quick setup

Open your `.env` file and set these four lines:

```env
GENERATION_LLM_ENABLED=true
GENERATION_LLM_PROVIDER=anthropic
GENERATION_LLM_MODEL=claude-sonnet-4-20250514
ANTHROPIC_API_KEY=sk-ant-your-key-here
```

Then restart:
```bash
docker compose restart backend worker
```

---

## Supported providers

### Anthropic (Claude)

```env
GENERATION_LLM_PROVIDER=anthropic
GENERATION_LLM_MODEL=claude-sonnet-4-20250514
ANTHROPIC_API_KEY=sk-ant-...
```

Get a key at https://console.anthropic.com/

This gives the best quality questions in my testing — rubrics are more detailed and the model answers are generally more accurate. Slightly slower and more expensive than the others.

### OpenAI (GPT)

```env
GENERATION_LLM_PROVIDER=openai
GENERATION_LLM_MODEL=gpt-4o-mini
OPENAI_API_KEY=sk-...
```

Get a key at https://platform.openai.com/api-keys

Good quality, fast, and cheaper than Claude. `gpt-4o-mini` is what I'd recommend for most use — `gpt-4o` is better but costs more.

### Google Gemini

```env
GENERATION_LLM_PROVIDER=gemini
GENERATION_LLM_MODEL=gemini-2.5-flash
GEMINI_API_KEY=...
```

Get a key at https://aistudio.google.com/app/apikeys

The cheapest option by a long way. Quality is decent — probably fine for most question types. I'd use this if you're generating a lot of questions and cost matters.

---

## Rough comparison

| | Claude Sonnet | GPT-4o Mini | Gemini 2.5 Flash |
|---|---|---|---|
| Quality | Best | Good | Good |
| Rubric detail | Best | Good | Decent |
| Speed per chunk | ~5–8s | ~3–4s | ~2–4s |
| Cost per 100 questions | ~$0.30–0.60 | ~$0.05–0.10 | ~$0.01–0.05 |

Honestly if you're just testing or don't have a budget constraint, start with Claude. If you're going to generate thousands of questions regularly, Gemini makes more sense financially.

---

## Local fallback (no internet required)

Set `GENERATION_LLM_ENABLED=false` and the system falls back to running qwen2:0.5b locally through Ollama.

```env
GENERATION_LLM_ENABLED=false
```

You'll need to pull the model first:
```bash
docker compose exec llm ollama pull qwen2:0.5b
```

The quality is noticeably worse — local generation tends to produce shorter model answers, looser rubrics, and sometimes returns malformed JSON that the fallback parser has to rescue. It's also slower on CPU (10–30s per chunk). But it works without any API key and keeps everything local.

---

## How it works internally

In `llm_service.py`, a `generation_service` object is created at startup based on your `.env` settings. All four clients (Anthropic, OpenAI, Gemini, Ollama) have the same `.generate(prompt)` interface, so the rest of the code doesn't need to care which one is being used. You can swap providers just by changing `.env` and restarting.

---

## Common problems

**"ANTHROPIC_API_KEY is not set"** — add it to `.env` and run `docker compose restart backend worker`

**"Model not found"** — double-check `GENERATION_LLM_MODEL`. Valid examples:
- Claude: `claude-sonnet-4-20250514`
- OpenAI: `gpt-4o-mini`, `gpt-4o`
- Gemini: `gemini-2.5-flash`, `gemini-2.0-flash`

**Rate limit errors** — you're sending too many requests. Try reducing `count` or using a provider with higher rate limits. The Gemini client has automatic retry with backoff built in.

**Generation comes back empty** — usually means the LLM returned something that couldn't be parsed as JSON. Check `docker compose logs backend` to see the raw response.

**Too slow with local qwen2:0.5b** — this is a CPU bottleneck. Either switch to an online provider or uncomment the GPU block in `docker-compose.yml` if you have an Nvidia GPU available.
