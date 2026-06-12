"""
Tests for sensitivity-at-ingestion (audit P1-01).

The meeting tier is stamped on each child (task/decision/open_question) ATOMICALLY
at insert, not only via a post-insert propagate pass that can silently fail and
leave CEO content at the DB default ('normal' -> founders/team-visible). propagate
stays as belt-and-suspenders but now reports which tables failed so callers alert.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _client_with_insert_capture():
    from services.supabase_client import SupabaseClient

    client = SupabaseClient.__new__(SupabaseClient)
    mock_internal = MagicMock()
    object.__setattr__(client, "_client", mock_internal)
    client.get_areas = MagicMock(return_value=[])  # used by create_tasks_batch
    mock_table = MagicMock()
    mock_internal.table.return_value = mock_table
    mock_table.insert.return_value.execute.return_value = MagicMock(data=[])
    return client, mock_table


class TestAtomicSensitivityAtInsert:
    def test_create_tasks_batch_stamps_meeting_tier(self):
        client, mock_table = _client_with_insert_capture()
        client.create_tasks_batch("m-1", [{"title": "T1"}, {"title": "T2"}], sensitivity="ceo")
        rows = mock_table.insert.call_args[0][0]
        assert rows and all(r["sensitivity"] == "ceo" for r in rows)

    def test_create_decisions_batch_stamps_meeting_tier(self):
        client, mock_table = _client_with_insert_capture()
        client.create_decisions_batch("m-1", [{"description": "D1"}], sensitivity="ceo")
        rows = mock_table.insert.call_args[0][0]
        assert rows and all(r["sensitivity"] == "ceo" for r in rows)

    def test_create_open_questions_batch_stamps_meeting_tier(self):
        client, mock_table = _client_with_insert_capture()
        client.create_open_questions_batch("m-1", [{"question": "Q1"}], sensitivity="ceo")
        rows = mock_table.insert.call_args[0][0]
        assert rows and all(r["sensitivity"] == "ceo" for r in rows)

    def test_no_sensitivity_arg_omits_field_backcompat(self):
        # Callers that don't pass a tier keep the prior DB-default behaviour.
        client, mock_table = _client_with_insert_capture()
        client.create_open_questions_batch("m-1", [{"question": "Q1"}])
        rows = mock_table.insert.call_args[0][0]
        assert "sensitivity" not in rows[0]


class TestStoreMeetingDataThreadsTier:
    @pytest.mark.asyncio
    async def test_store_meeting_data_passes_sensitivity_to_batches(self):
        import processors.transcript_processor as tp

        with patch.object(tp, "supabase_client") as mock_sc:
            await tp.store_meeting_data(
                meeting_id="m-1",
                decisions=[{"description": "d"}],
                tasks=[{"title": "t"}],
                follow_ups=[],
                open_questions=[{"question": "q"}],
                sensitivity="ceo",
            )

        assert mock_sc.create_decisions_batch.call_args.kwargs["sensitivity"] == "ceo"
        assert mock_sc.create_tasks_batch.call_args.kwargs["sensitivity"] == "ceo"
        assert mock_sc.create_open_questions_batch.call_args.kwargs["sensitivity"] == "ceo"


class TestPropagateSurfacesFailures:
    def test_propagate_reports_failed_table(self):
        from guardrails.sensitivity_classifier import propagate_meeting_sensitivity

        mock_sc = MagicMock()

        def _table(name):
            t = MagicMock()
            chain = t.update.return_value.eq.return_value
            if name == "tasks":
                chain.execute.side_effect = Exception("db down")
            else:
                chain.execute.return_value = MagicMock(data=[{"id": "x"}])
            return t

        mock_sc.client.table.side_effect = _table
        with patch("services.supabase_client.supabase_client", mock_sc):
            result = propagate_meeting_sensitivity("m-1", "ceo")

        assert result["failed_tables"] == ["tasks"]
        # the other two still propagated
        assert result["decisions"] == 1 and result["open_questions"] == 1

    def test_propagate_no_failures_returns_empty(self):
        from guardrails.sensitivity_classifier import propagate_meeting_sensitivity

        mock_sc = MagicMock()
        mock_sc.client.table.return_value.update.return_value.eq.return_value.execute.return_value = MagicMock(
            data=[{"id": "x"}]
        )
        with patch("services.supabase_client.supabase_client", mock_sc):
            result = propagate_meeting_sensitivity("m-1", "ceo")

        assert result["failed_tables"] == []
