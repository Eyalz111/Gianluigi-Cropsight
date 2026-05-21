"""
v2.5 PR2 cold-start — one-shot synthesis of initial topic + area briefs.

Synthesizes a TopicBrief for every topic_thread that lacks one (Sonnet), then
an AreaBrief per Area aggregating its child topic briefs (Opus). Idempotent:
re-runs skip topics/areas that already have a brief unless --force.

HARD GATE (plan #e): before a broad run, confirm the real Area list/count from
the live Gantt:
    SELECT DISTINCT section FROM gantt_schema;   -- exclude OPERATIONAL RULES,
    STRATEGIC MILESTONES, Meeting Cadence, _config/_metadata
Then hand-validate 5-10 topics first:
    python scripts/synthesize_initial_briefs.py --topics-only --limit 8 --dry-run

Prereqs: migrate_phase_v25_knowledge.sql applied + backfill_knowledge_v25.py run.

Usage:
    python scripts/synthesize_initial_briefs.py [--limit N] [--dry-run]
        [--force] [--topics-only] [--areas-only] [--no-rag]
"""

import argparse
import asyncio
import logging
import os
import sys

if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


async def _run(args) -> dict:
    from processors.knowledge_synthesis import synthesize_all_areas, synthesize_all_topics

    out: dict = {}
    if not args.areas_only:
        out["topics"] = await synthesize_all_topics(
            limit=args.limit,
            force=args.force,
            use_rag=not args.no_rag,
            dry_run=args.dry_run,
        )
    if not args.topics_only:
        out["areas"] = await synthesize_all_areas(force=args.force, dry_run=args.dry_run)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Cold-start knowledge brief synthesis (v2.5 PR2)")
    parser.add_argument("--limit", type=int, default=None, help="Max topics to synthesize (hand-validation)")
    parser.add_argument("--dry-run", action="store_true", help="Synthesize + log, but do NOT write to DB")
    parser.add_argument("--force", action="store_true", help="Re-synthesize even if a brief already exists")
    parser.add_argument("--topics-only", action="store_true", help="Synthesize topic briefs only")
    parser.add_argument("--areas-only", action="store_true", help="Synthesize area briefs only")
    parser.add_argument("--no-rag", action="store_true", help="Skip semantic-chunk enrichment")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    mode = "DRY-RUN" if args.dry_run else "WRITE"
    print(f"Cold-start brief synthesis [{mode}] "
          f"(limit={args.limit}, force={args.force}, rag={not args.no_rag})...")
    result = asyncio.run(_run(args))
    print(f"Done: {result}")


if __name__ == "__main__":
    main()
