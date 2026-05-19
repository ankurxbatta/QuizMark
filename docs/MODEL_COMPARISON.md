# Local vs online LLM for generation

I went back and forth on this during the project. Here's what I found.

---

## The main trade-off

Online models (Claude, GPT, Gemini) produce noticeably better questions — more accurate model answers, better rubrics, more consistent JSON output. But they cost money and send your data to a third party.

Local models via Ollama are free and keep everything on your own machine, but question quality is worse and it's slow on CPU.

For this project I defaulted to online because the whole point is generating good questions, and the cost at the scale we're talking about (a few hundred questions) is minimal. But I built the local fallback in case someone needs to run it without internet access or has concerns about data privacy.

---

## What I tested

I compared output from three providers against local qwen2:0.5b on the same set of chapters.

**Short answer questions:**
Online models gave cleaner, more precise answers. qwen2:0.5b tended to be either too brief or started rambling. The rubrics from online models were better structured — proper one-criterion-per-mark format rather than vague descriptions.

**MCQ:**
Online models were much better at writing plausible distractors. qwen2:0.5b often produced obviously wrong options that any student could eliminate immediately.

**True/false:**
Similar quality actually — this is a simpler task and phi3:mini handled it reasonably well.

**JSON reliability:**
This was the biggest practical difference. Online models almost always return clean JSON. phi3:mini would frequently include prose before or after the JSON array, or occasionally produce malformed JSON entirely. I built a fallback parser that strips markdown fences and rescues partial arrays, but it still fails sometimes and triggers the deterministic fallback.

---

## Rough quality estimate

Based on reviewing about 200 generated questions:

| | phi3:mini (local) | Claude Sonnet | GPT-4o Mini | Gemini Flash |
|---|---|---|---|---|
| Questions needing significant edits | ~40–50% | ~5–10% | ~10–15% | ~15–20% |
| JSON parse failures | ~10–15% | <1% | <1% | 1–2% |

These are rough — it varies a lot depending on the source material.

---

## My recommendation

If you have budget for API calls, use an online provider. At a few thousand questions per year the cost is probably under $20–50 depending on which provider. The reduction in editing time is worth it.

If you need local-only (privacy, no internet, institutional policy), phi3:mini works but plan to review and edit more of the output. Using a larger local model like llama3 would probably help but I haven't tested it for generation specifically — only marking.

---

## Switching providers

It's just an environment variable change, no code changes needed:

```env
# Switch to Gemini
GENERATION_LLM_PROVIDER=gemini
GENERATION_LLM_MODEL=gemini-2.5-flash
GEMINI_API_KEY=your-key

# Switch to local
GENERATION_LLM_ENABLED=false
```

Then `docker compose restart backend worker`.
