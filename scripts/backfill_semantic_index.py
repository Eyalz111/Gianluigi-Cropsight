"""Backfill the semantic index for decisions + topics (Phase 1).

Embeds all approved current decisions + active topics-with-narratives into the
shared `embeddings` table (source_type='decision'/'topic', delete-then-insert,
idempotent — safe to re-run). Batches via embedding_service.embed_texts.

Runs unconditionally (does NOT require SEMANTIC_INDEX_ENABLED) — the flag only
gates the incremental lifecycle hooks. Intended order:
  1. Eyal runs scripts/migrate_semantic_index.sql in Supabase.
  2. Deploy dark.
  3. Run this with --apply.
  4. Flip SEMANTIC_INDEX_ENABLED=true.

    # preview candidate counts (no writes / no embed calls)
    PYTHONPATH=. python scripts/backfill_semantic_index.py --decisions --topics
    # actually embed + write
    PYTHONPATH=. python scripts/backfill_semantic_index.py --decisions --topics --apply
"""
import argparse
import asyncio

from processors.semantic_index import backfill_decisions, backfill_topics


async def main(do_decisions: bool, do_topics: bool, apply: bool) -> None:
    if not (do_decisions or do_topics):
        print("nothing selected — pass --decisions and/or --topics (add --apply to write)")
        return
    if do_decisions:
        print("decisions:", await backfill_decisions(apply=apply), flush=True)
    if do_topics:
        print("topics:", await backfill_topics(apply=apply), flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Backfill the decision/topic semantic index.")
    ap.add_argument("--decisions", action="store_true", help="index approved current decisions")
    ap.add_argument("--topics", action="store_true", help="index active topics with a narrative")
    ap.add_argument("--apply", action="store_true", help="actually embed + write (default = dry-run counts)")
    args = ap.parse_args()
    asyncio.run(main(args.decisions, args.topics, args.apply))
