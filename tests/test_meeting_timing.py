"""When Eyal wants a meeting, in his own words.

This exists because six of his meetings were being discarded every thirty
minutes. `proposed_date` is a timestamptz, he writes "Once a week" into that
column, Postgres raised 22007, a broad except returned None, and the caller did
`if not created: continue` — losing the whole row, not just the cell.

So the tests that matter most here are the ones asserting a row SURVIVES text
the parser cannot read.
"""
from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from processors.meeting_timing import parse_single_date, parse_timing, sort_key

TODAY = date(2026, 8, 12)


class TestTheTextIsNeverLost:
    @pytest.mark.parametrize("text", [
        "Once a week", "every 2 weeks", "end of August",
        "when Paolo is back from Milan", "ASAP", "15/9",
    ])
    def test_whatever_was_typed_comes_back_verbatim(self, text):
        """timing_text is what Nechama reads. The system has no business
        rewriting it, and a parser round-trip would eventually turn "end of
        August, before Paolo travels" into a date and lose the reason."""
        assert parse_timing(text, TODAY)["text"] == text

    def test_unreadable_text_is_a_valid_answer_not_a_failure(self):
        out = parse_timing("when Paolo is back from Milan", TODAY)
        assert out["kind"] == "unknown"
        assert out["date"] is None
        assert out["text"] == "when Paolo is back from Milan"


class TestRecurrence:
    @pytest.mark.parametrize("text,expected", [
        ("Once a week", "weekly"), ("every week", "weekly"), ("weekly", "weekly"),
        ("every 2 weeks", "biweekly"), ("Bi Weekly", "biweekly"),
        ("every other week", "biweekly"), ("fortnightly", "biweekly"),
        ("once a month", "monthly"), ("Monthly", "monthly"),
        ("quarterly", "quarterly"), ("yearly", "annual"),
        ("twice a week", "twice_weekly"), ("every 2 months", "bimonthly"),
    ])
    def test_the_cadences_he_actually_writes(self, text, expected):
        out = parse_timing(text, TODAY)
        assert out["kind"] == "recurrence"
        assert out["recurrence"] == expected

    def test_every_2_weeks_is_not_eaten_by_every_week(self):
        """Ordering bug waiting to happen: a looser "every ... week" pattern
        matched first would silently downgrade a fortnightly meeting."""
        assert parse_timing("every 2 weeks", TODAY)["recurrence"] == "biweekly"
        assert parse_timing("every week", TODAY)["recurrence"] == "weekly"

    def test_a_cadence_with_a_start_date_is_still_a_cadence(self):
        """"every week from 15/9" is a rhythm with a start, not one booking."""
        out = parse_timing("every week from 15/9/2026", TODAY)
        assert out["kind"] == "recurrence" and out["recurrence"] == "weekly"


class TestSingleDates:
    @pytest.mark.parametrize("text,iso", [
        ("15/09/2026", "2026-09-15"), ("2026-09-15", "2026-09-15"),
        ("15.09.2026", "2026-09-15"), ("15 Sep 2026", "2026-09-15"),
    ])
    def test_the_written_forms(self, text, iso):
        assert parse_single_date(text) == iso

    def test_a_bare_day_month_is_read_as_a_date(self):
        out = parse_timing("15/9", TODAY)
        assert out["kind"] == "date" and out["date"].endswith("-09-15")


class TestWindows:
    def test_a_range(self):
        out = parse_timing("10/9/2026 - 20/9/2026", TODAY)
        assert out["kind"] == "window"
        assert out["window_start"] == "2026-09-10"
        assert out["window_end"] == "2026-09-20"

    def test_week_of(self):
        out = parse_timing("week of 16/9/2026", TODAY)   # a Wednesday
        assert out["kind"] == "window"
        assert out["window_start"] == "2026-09-14"       # back to Monday
        assert out["window_end"] == "2026-09-20"

    def test_end_of_a_month(self):
        out = parse_timing("end of August", TODAY)
        assert out["kind"] == "window"
        assert out["window_end"] == "2026-08-31"

    def test_a_bare_month_is_the_whole_month(self):
        out = parse_timing("September", TODAY)
        assert out["window_start"] == "2026-09-01"
        assert out["window_end"] == "2026-09-30"

    def test_a_month_already_past_means_next_year(self):
        out = parse_timing("February", TODAY)
        assert out["window_start"] == "2027-02-01"

    def test_a_quarter(self):
        out = parse_timing("Q4 2026", TODAY)
        assert out["window_start"] == "2026-10-01"
        assert out["window_end"] == "2026-12-31"


class TestSorting:
    def test_a_recurrence_sorts_first_not_as_undated(self):
        """A weekly meeting has no single date, but it is the most live thing on
        the tab — sorting it into NO DATE trains the eye to ignore that bucket."""
        rec = sort_key(parse_timing("Once a week", TODAY))
        dated = sort_key(parse_timing("15/9/2026", TODAY))
        unknown = sort_key(parse_timing("ask Paolo", TODAY))
        assert rec < dated < unknown

    def test_a_window_sorts_by_when_it_opens(self):
        early = sort_key(parse_timing("week of 7/9/2026", TODAY))
        late = sort_key(parse_timing("week of 21/9/2026", TODAY))
        assert early < late


class TestTheRowSurvives:
    """The actual defect: a cell that will not parse must not cost the meeting."""

    def _create(self, proposed, raise_on_timing=False):
        from services.supabase_client import supabase_client

        sent = {}

        def _insert(payload):
            t = MagicMock()
            if raise_on_timing and "timing_text" in payload:
                t.execute.side_effect = Exception(
                    "column follow_up_meetings.timing_text does not exist (42703)")
            else:
                sent["payload"] = payload
                t.execute.return_value = MagicMock(data=[{"id": "m1", **payload}])
            return t

        table = MagicMock()
        table.insert.side_effect = _insert
        client = MagicMock()
        client.table.return_value = table

        with patch.object(type(supabase_client), "client",
                          property(lambda self: client)), \
             patch.object(supabase_client, "resolve_assignee", lambda v: v), \
             patch.object(supabase_client, "resolve_label", lambda v: v):
            out = supabase_client.create_follow_up_meeting_manual(
                title="CropSight Weekly Managment Meeting", led_by="Eyal Zror",
                proposed_date=proposed, participants=[], label="Managment",
                status="recurring", priority="H")
        return out, sent.get("payload", {})

    def test_once_a_week_creates_the_meeting(self):
        """The exact row that was being dropped every cycle."""
        out, payload = self._create("Once a week")
        assert out is not None, "the meeting was dropped again"
        assert payload["timing_text"] == "Once a week"
        assert payload["recurrence"] == "weekly"
        assert payload["proposed_date"] is None, "no date was claimed"

    def test_free_text_creates_the_meeting(self):
        out, payload = self._create("when Paolo is back")
        assert out is not None
        assert payload["timing_text"] == "when Paolo is back"
        assert payload["proposed_date"] is None
        assert "recurrence" not in payload

    def test_a_real_date_still_lands_as_a_date(self):
        out, payload = self._create("15/09/2026")
        assert payload["proposed_date"] == "2026-09-15"

    def test_a_window_is_stored_as_a_window(self):
        out, payload = self._create("end of August")
        assert payload["window_start"] and payload["window_end"] == "2026-08-31"

    def test_it_still_creates_before_the_migration_has_run(self):
        """Insisting on the new columns would drop the row for a NEW reason —
        the same failure this change exists to fix."""
        out, payload = self._create("Once a week", raise_on_timing=True)
        assert out is not None, "the row was dropped when the columns were absent"
        assert "timing_text" not in payload
        assert payload["title"] == "CropSight Weekly Managment Meeting"


class TestTheUpdatePathAlsoKeepsTheWords:
    """An EXISTING meeting whose Proposed Date cell is changed to a phrase. The
    update path already refused to pull it as a date — correctly — but it also
    never stored it, so the words stayed sheet-only and nothing else in the
    system could see them."""

    def _update(self, updates, columns_absent=False):
        from services.supabase_client import supabase_client

        applied = {}

        def _update_call(payload):
            t = MagicMock()
            if columns_absent and any(k in payload for k in
                                      ("timing_text", "recurrence")):
                t.eq.return_value.execute.side_effect = Exception(
                    "Could not find the 'timing_text' column (PGRST204)")
            else:
                applied["payload"] = payload
                t.eq.return_value.execute.return_value = MagicMock(
                    data=[{"id": "m1", **payload}])
            return t

        table = MagicMock()
        table.update.side_effect = _update_call
        client = MagicMock()
        client.table.return_value = table

        with patch.object(type(supabase_client), "client",
                          property(lambda self: client)), \
             patch.object(supabase_client, "resolve_label", lambda v: v), \
             patch.object(supabase_client, "resolve_assignee", lambda v: v):
            out = supabase_client.update_follow_up_meeting("m1", **updates)
        return out, applied.get("payload", {})

    def test_the_phrase_reaches_the_database(self):
        _, payload = self._update({"timing_text": "every 2 weeks",
                                   "recurrence": "biweekly"})
        assert payload["timing_text"] == "every 2 weeks"
        assert payload["recurrence"] == "biweekly"

    def test_it_degrades_before_the_migration_rather_than_raising(self):
        """update_follow_up_meeting RAISES on failure, so insisting on the new
        columns would take the whole meetings reconcile down with it — a worse
        failure than the one it is carrying."""
        out, payload = self._update(
            {"timing_text": "Once a week", "recurrence": "weekly",
             "status": "recurring"},
            columns_absent=True)
        assert out is not None
        assert "timing_text" not in payload
        assert payload["status"] == "recurring", "the real edit still applied"

    def test_an_unrelated_failure_still_raises(self):
        """The fallback must not swallow a genuine error."""
        from services.supabase_client import supabase_client
        table = MagicMock()
        table.update.return_value.eq.return_value.execute.side_effect = \
            Exception("permission denied for table follow_up_meetings")
        client = MagicMock()
        client.table.return_value = table
        with patch.object(type(supabase_client), "client",
                          property(lambda self: client)), \
             patch.object(supabase_client, "resolve_label", lambda v: v), \
             patch.object(supabase_client, "resolve_assignee", lambda v: v):
            with pytest.raises(Exception, match="permission denied"):
                supabase_client.update_follow_up_meeting("m1", status="held")
