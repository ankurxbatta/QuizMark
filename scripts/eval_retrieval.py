#!/usr/bin/env python3
"""
eval_retrieval.py — score routed retrieval against a golden-query set
(Phase 4 of MULTI_RAG_DESIGN).

Run from project root (needs .env with DB + embedding keys; costs one
embedding call per query — a fraction of a cent for a typical set):

    python3 scripts/eval_retrieval.py --golden eval/golden/my_book.json [--k 8] [--json out.json]

A template golden set lives at eval/golden/example.json. Compare runs
before/after retrieval changes (e.g. RERANK_ENABLED=true vs false) to see
whether a change actually helps.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_REPO_ROOT / "backend"))

_ENV_FILE = _REPO_ROOT / ".env"
if _ENV_FILE.exists():
    from dotenv import load_dotenv

    load_dotenv(_ENV_FILE)


def _print_summary(report: dict) -> None:
    print(f"\nGolden set: book_id={report['book_id']}  queries={report['num_queries']}  k={report['k']}")
    print(f"{'index':<10} {'queries':>7} {'hit@k':>7} {'mrr':>7} {'ndcg@k':>7}")
    for name in ("text", "formula", "figure", "table", "fused"):
        row = report["summary"].get(name)
        if not row:
            continue
        print(f"{name:<10} {row['queries']:>7} {row['hit@k']:>7.3f} {row['mrr']:>7.3f} {row['ndcg@k']:>7.3f}")
    print()
    worst = sorted(report["per_query"], key=lambda d: d["scores"]["fused"]["mrr"])[:3]
    if worst:
        print("Weakest queries (fused MRR):")
        for item in worst:
            print(f"  {item['scores']['fused']['mrr']:.3f}  {item['query'][:80]}")


async def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--golden", required=True, help="path to golden-query JSON")
    parser.add_argument("--k", type=int, default=8)
    parser.add_argument("--json", help="also write the full report to this path")
    args = parser.parse_args()

    golden = json.loads(Path(args.golden).read_text())

    from app.services.retrieval_eval import evaluate_golden

    report = await evaluate_golden(golden, k=args.k)
    _print_summary(report)
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2, default=str))
        print(f"Full report → {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
