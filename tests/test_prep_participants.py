"""The prep was empty because it asked for names that could never match.

`generate_prep_outline` derived participants as
`displayName or email.split("@")[0]`. Google returns an EMPTY displayName for
our own people, so it queried for "nechama" and "eyal.zror" while every
assignee is stored canonically as "Nechama Tik" and "Eyal Zror" — and
`get_tasks` matches assignee with `ilike` and no wildcards.

On 2026-08-11 the two attendees of the next day's meeting had 29 open tasks
between them and the prep found ZERO. Every section rendered `item_count: 0`
with `status: "ok"`, which is exactly why Eyal said the prep messages read as
disconnected from anything.
"""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import processors.meeting_prep as mp

ROSTER = ["Eyal Zror", "Nechama Tik", "Roye Tadmor"]


def _members(email):
    return {
        "nechama@cropsight.io": {"name": "Nechama Tik"},
        "eyal.zror@cropsight.io": {"name": "Eyal Zror"},
    }.get((email or "").lower())


class TestParticipantNames:
    def _run(self, attendees, resolve=lambda v: v):
        with patch.object(mp, "supabase_client",
                          SimpleNamespace(resolve_assignee=resolve)), \
             patch("config.team.get_team_member_by_email", side_effect=_members), \
             patch("config.team.get_team_member_names", return_value=ROSTER):
            return mp.participant_names_from_attendees(attendees)

    def test_an_empty_display_name_no_longer_becomes_the_email_local_part(self):
        """The exact shape Google returns for our own people."""
        out = self._run([
            {"email": "nechama@cropsight.io", "displayName": ""},
            {"email": "eyal.zror@cropsight.io", "displayName": ""},
        ])
        assert out == ["Nechama Tik", "Eyal Zror"]
        assert "nechama" not in out and "eyal.zror" not in out

    def test_the_email_wins_over_a_display_name(self):
        """The address is the reliable identity in a calendar event."""
        out = self._run([{"email": "eyal.zror@cropsight.io", "displayName": "eyal"}])
        assert out == ["Eyal Zror"]

    def test_a_display_name_is_used_when_the_email_is_unknown(self):
        out = self._run([{"email": "personal@gmail.com", "displayName": "Roye"}],
                        resolve=lambda v: "Roye Tadmor" if v == "Roye" else v)
        assert out == ["Roye Tadmor"]

    def test_an_external_guest_is_dropped(self):
        """`resolve_assignee` returns unknown input UNCHANGED by design, so
        without a roster check a guest's name became a 'participant' we then
        queried tasks for."""
        out = self._run([{"email": "investor@example.com",
                          "displayName": "Some Investor"}])
        assert out == []

    def test_duplicates_collapse(self):
        out = self._run([
            {"email": "eyal.zror@cropsight.io", "displayName": ""},
            {"email": "eyal.zror@cropsight.io", "displayName": "Eyal"},
        ])
        assert out == ["Eyal Zror"]

    def test_no_attendees_is_not_a_crash(self):
        assert self._run([]) == []
        assert self._run(None) == []


class TestOpenMeansOpen:
    @pytest.mark.asyncio
    async def test_in_progress_tasks_are_included(self):
        """The section is called "All Open Tasks" but asked for status=pending
        only — 28 of Eyal's 39 open tasks on the day this was found."""
        calls = []

        def _get_tasks(assignee=None, status=None, **kw):
            calls.append(status)
            return {"pending": [{"id": "1", "title": "p"}],
                    "in_progress": [{"id": "2", "title": "ip"}],
                    "overdue": []}.get(status, [])

        with patch.object(mp, "supabase_client",
                          SimpleNamespace(get_tasks=_get_tasks)), \
             patch.object(mp, "filter_by_sensitivity", lambda rows, lvl: rows):
            out = await mp.find_participant_tasks(["Eyal Zror"])

        assert {"pending", "in_progress"} <= set(calls)
        assert len(out["Eyal Zror"]) == 2

    @pytest.mark.asyncio
    async def test_a_row_returned_twice_is_counted_once(self):
        def _get_tasks(assignee=None, status=None, **kw):
            return [{"id": "same", "title": "x"}]

        with patch.object(mp, "supabase_client",
                          SimpleNamespace(get_tasks=_get_tasks)), \
             patch.object(mp, "filter_by_sensitivity", lambda rows, lvl: rows):
            out = await mp.find_participant_tasks(["Eyal Zror"])
        assert len(out["Eyal Zror"]) == 1

    @pytest.mark.asyncio
    async def test_a_row_without_an_id_is_kept_not_dropped(self):
        """De-duping on a missing id is how a previous fix silently lost rows."""
        def _get_tasks(assignee=None, status=None, **kw):
            return [{"title": "no id"}] if status == "pending" else []

        with patch.object(mp, "supabase_client",
                          SimpleNamespace(get_tasks=_get_tasks)), \
             patch.object(mp, "filter_by_sensitivity", lambda rows, lvl: rows):
            out = await mp.find_participant_tasks(["Eyal Zror"])
        assert len(out["Eyal Zror"]) == 1
