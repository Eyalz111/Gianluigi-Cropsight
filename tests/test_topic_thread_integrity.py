"""
Tests for topic-thread integrity (audit P1-07): meeting_count reflects the true
DISTINCT mention set (no blind +1 inflation), and mentions are idempotent per
(topic_id, meeting_id) so a re-extraction can't fabricate "discussed in N meetings".
"""

from unittest.mock import MagicMock, patch

import pytest


def _chain(execute_data):
    """A self-returning supabase-style chain whose .execute().data is fixed."""
    chain = MagicMock()
    for m in ("table", "select", "insert", "update", "delete", "eq", "order"):
        getattr(chain, m).return_value = chain
    chain.execute.return_value = MagicMock(data=execute_data)
    return chain


class TestMeetingCountFromDistinct:
    def test_distinct_mention_count_dedupes(self):
        from processors import topic_threading as tt
        chain = _chain([
            {"meeting_id": "m1"}, {"meeting_id": "m1"},
            {"meeting_id": "m2"}, {"meeting_id": None},
        ])
        with patch.object(tt.supabase_client, "_client", chain):
            assert tt._distinct_mention_count("t1") == 2  # m1+m2; dup + None ignored

    def test_update_thread_uses_distinct_not_blind_increment(self):
        from processors import topic_threading as tt
        chain = _chain([{"meeting_id": "m1"}, {"meeting_id": "m2"}, {"meeting_id": "m3"}])
        with patch.object(tt.supabase_client, "_client", chain):
            # Stored count is a stale/inflated 99 — the recompute must override it.
            tt._update_thread_for_meeting({"id": "t1", "meeting_count": 99}, "m3")
        payload = chain.update.call_args.args[0]
        assert payload["meeting_count"] == 3  # distinct count, NOT 99+1
        assert payload["last_meeting_id"] == "m3"


class TestMentionIdempotency:
    def test_create_mention_replaces_existing_pair(self):
        from processors import topic_threading as tt
        chain = _chain([])
        with patch.object(tt.supabase_client, "_client", chain):
            tt._create_mention("t1", "m1", decisions=[], tasks=[], label="Moldova")
        # Idempotent: it deletes any prior (topic_id, meeting_id) mention, then inserts.
        assert chain.delete.called, "must delete the prior mention for this pair first"
        assert chain.insert.called, "must insert the fresh mention"
