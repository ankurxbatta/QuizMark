# Model Comparison

Tested on `IntroductoryBusinessStatistics-OP.pdf` (631 pages, 13 chapters).

---

## Embedding Models

| Model | Dimensions | Provider | Cost | Notes |
|---|---|---|---|---|
| `gemini-embedding-001` | 768 | Gemini (free) | Free quota | Primary. MongoDB index built on this. |
| `text-embedding-3-small` | 768* | OpenAI (paid) | $0.02/1M tokens | Fallback. `dimensions=768` keeps index compatible. |

*`text-embedding-3-small` natively produces 1536-dim but is configured with `dimensions=768` so both providers are interchangeable without rebuilding the MongoDB vector index.

---

## Vision Models (Chart Descriptions)

| Model | Provider | Cost | Quality | Speed |
|---|---|---|---|---|
| `gpt-4o-mini` | OpenAI | $0.15/1M in · $0.60/1M out | ★★★★☆ | Fast |
| `claude-haiku-4-5-20251001` | Anthropic | $0.80/1M in · $4/1M out | ★★★★☆ | Fast |
| `gemini-2.5-flash` | Gemini | Free quota | ★★★☆☆ | Fast |

**gpt-4o-mini** is used as primary. It reliably identifies histograms, scatter plots, frequency tables, and describes axis labels and data trends accurately.

---

## Generation Models

| Model | Provider | Cost | Quality | Notes |
|---|---|---|---|---|
| `gpt-4o-mini` | OpenAI | $0.15/1M in · $0.60/1M out | ★★★★☆ | Primary. Good at JSON output and Bloom's taxonomy. |
| `claude-haiku-4-5-20251001` | Anthropic | $0.80/1M in · $4/1M out | ★★★★☆ | Fallback. Reliable structured output. |
| `gpt-4o` | OpenAI | $2.50/1M in · $10/1M out | ★★★★★ | Higher quality, 16× more expensive. Use for critical question banks. |
| `claude-sonnet-4-6` | Anthropic | $3/1M in · $15/1M out | ★★★★★ | Higher quality fallback. |

**gpt-4o-mini** produces diverse, pedagogically sound questions with correct Bloom's classification and usable rubrics in the vast majority of cases. The quality difference vs gpt-4o is small for straightforward textbook content.

---

## Marking Models

| Model | Provider | Cost per submission | Notes |
|---|---|---|---|
| `gpt-4o-mini` | OpenAI | ~$0.002 | Primary. Good at rubric-following. |
| `claude-haiku-4-5-20251001` | Anthropic | ~$0.002 | Fallback. Very precise instruction-following. |

Both models reliably follow the rubric structure and produce consistent scores when given clear rubrics. The SLM pre-scorer skips the LLM entirely for high-confidence matches (~30-40% of submissions), reducing cost.

---

## Math Extraction

| Model | Provider | Quality | Notes |
|---|---|---|---|
| `gpt-4o-mini` (vision) | OpenAI | ★★★★☆ | Reliably extracts LaTeX from rendered page images. |
| `claude-haiku-4-5-20251001` (vision) | Anthropic | ★★★★☆ | Good LaTeX output on complex formulas. |
