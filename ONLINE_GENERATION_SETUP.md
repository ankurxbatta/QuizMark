# Setting up online generation

Quick guide for getting the online LLM working for question generation.

The system defaults to using an online provider (Claude, GPT, or Gemini) because local models are too slow and produce worse output. You just need an API key from whichever provider you want to use.

---

## Steps

1. Get an API key from one of:
   - Anthropic (Claude): https://console.anthropic.com/
   - OpenAI (GPT): https://platform.openai.com/api-keys
   - Google (Gemini): https://aistudio.google.com/app/apikeys

2. Open your `.env` file and set:

```env
GENERATION_LLM_ENABLED=true
GENERATION_LLM_PROVIDER=anthropic    # or openai / gemini
GENERATION_LLM_MODEL=claude-sonnet-4-20250514
ANTHROPIC_API_KEY=sk-ant-your-key
```

3. Restart the backend:
```bash
docker compose restart backend worker
```

4. Test it by uploading a short text file and generating a few questions.

For more detail on comparing providers and switching between them, see [docs/GENERATION_LLM.md](docs/GENERATION_LLM.md).
