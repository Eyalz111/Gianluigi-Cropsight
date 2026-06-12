"""
Tests for follow-up-meeting sensitivity (audit P1-05).

follow_up_meetings was the one extraction child table with no tier. P1-05 gives it
one — flag-gated (FOLLOW_UP_SENSITIVITY_ENABLED) because the column only exists
after scripts/migrate_followup_sensitivity_p1_05.sql is applied, so the code must
be safe to deploy BEFORE the migration (flag off = old behaviour, no missing-column
insert/UPDATE).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import config.settings as cfg


def _client():
    from services.supabase_client import SupabaseClient

    client = SupabaseClient.__new__(SupabaseClient)
    mock_internal = MagicMock()
    object.__setattr__(client, "_client", mock_internal)
    mock_table = MagicMock()
    mock_internal.table.return_value = mock_table
    mock_table.insert.return_value.execute.return_value = MagicMock(data=[])
    return client, mock_table


class TestCreateFollowUpsBatchTier:
    def test_stamps_tier_when_passed(self):
        client, mock_table = _client()
        client.create_follow_ups_batch(
            "m-1", [{"title": "Term sheet w/ VC", "led_by": "Eyal"}], sensitivity="ceo"
        )
        rows = mock_table.insert.call_args[0][0]
        assert rows and all(r["sensitivity"] == "ceo" for r in rows)

    def test_omits_tier_when_none(self):
        # Pre-migration safety: no `sensitivity` key, so the insert can't reference
        # a column that doesn't exist yet.
        client, mock_table = _client()
        client.create_follow_ups_batch("m-1", [{"title": "T", "led_by": "Eyal"}])
        rows = mock_table.insert.call_args[0][0]
        assert "sensitivity" not in rows[0]


class TestStoreMeetingDataFlagGate:
    @pytest.mark.asyncio
    async def test_passes_tier_when_flag_on(self):
        import processors.transcript_processor as tp

        with patch.object(tp, "supabase_client") as mock_sc, \
             patch.object(tp.settings, "FOLLOW_UP_SENSITIVITY_ENABLED", True):
            await tp.store_meeting_data(
                "m-1", decisions=[], tasks=[],
                follow_ups=[{"title": "f", "led_by": "E"}], open_questions=[],
                sensitivity="ceo",
            )
        assert mock_sc.create_follow_ups_batch.call_args.kwargs["sensitivity"] == "ceo"

    @pytest.mark.asyncio
    async def test_omits_tier_when_flag_off(self):
        import processors.transcript_processor as tp

        with patch.object(tp, "supabase_client") as mock_sc, \
             patch.object(tp.settings, "FOLLOW_UP_SENSITIVITY_ENABLED", False):
            await tp.store_meeting_data(
                "m-1", decisions=[], tasks=[],
                follow_ups=[{"title": "f", "led_by": "E"}], open_questions=[],
                sensitivity="ceo",
            )
        assert mock_sc.create_follow_ups_batch.call_args.kwargs["sensitivity"] is None


class TestPropagateFollowUpsFlagGate:
    def test_propagate_includes_followups_keyed_on_source_meeting(self):
        from guardrails.sensitivity_classifier import propagate_meeting_sensitivity

        mock_sc = MagicMock()
        seen = {}

        def _table(name):
            t = MagicMock()
            t.update.return_value.eq.return_value.execute.return_value = MagicMock(data=[{"id": "x"}])
            seen[name] = t
            return t

        mock_sc.client.table.side_effect = _table
        with patch("services.supabase_client.supabase_client", mock_sc), \
             patch.object(cfg.settings, "FOLLOW_UP_SENSITIVITY_ENABLED", True):
            result = propagate_meeting_sensitivity("m-1", "ceo")

        assert "follow_up_meetings" in seen
        # follow_up_meetings keys on source_meeting_id, not meeting_id
        seen["follow_up_meetings"].update.return_value.eq.assert_called_with(
            "source_meeting_id", "m-1"
        )
        assert result["failed_tables"] == []

    def test_propagate_skips_followups_when_flag_off(self):
        from guardrails.sensitivity_classifier import propagate_meeting_sensitivity

        mock_sc = MagicMock()
        seen = []

        def _table(name):
            seen.append(name)
            t = MagicMock()
            t.update.return_value.eq.return_value.execute.return_value = MagicMock(data=[{"id": "x"}])
            return t

        mock_sc.client.table.side_effect = _table
        with patch("services.supabase_client.supabase_client", mock_sc), \
             patch.object(cfg.settings, "FOLLOW_UP_SENSITIVITY_ENABLED", False):
            propagate_meeting_sensitivity("m-1", "ceo")

        assert "follow_up_meetings" not in seen
