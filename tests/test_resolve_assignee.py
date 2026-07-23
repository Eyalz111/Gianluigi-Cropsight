"""
resolve_assignee — canonicalize task/decision owners to FIRST + LAST name.

Why this exists: live production data carried the same person under two
spellings — "Eyal Zror" x31 AND "Eyal" x9, with Paolo/Roye/Yoram split the same
way. get_tasks filters with `ilike` and no wildcards, so asking "what does Paolo
owe?" returned 4 of 15 rows. That single fact makes a shared weekly review
impossible, which is why this landed before the office-manager workspace.

Contract mirrors resolve_category: canonicalize what we recognise, and RETURN
UNKNOWN VALUES AS-IS (never destroy what a human typed) — the QA pass
(_check_assignee_taxonomy) is the compensating control that surfaces them.
"""

import pytest


ROSTER = [
    {"name": "Eyal Zror"},
    {"name": "Roye Tadmor"},
    {"name": "Paolo Vailetti"},
    {"name": "Yoram Weiss"},
    {"name": "Nechama Tik"},
]


@pytest.fixture
def sc():
    try:
        from services.supabase_client import supabase_client
    except Exception as e:  # pragma: no cover
        pytest.skip(f"cannot import supabase_client ({e})")
    return supabase_client


class TestResolveAssignee:
    @pytest.mark.parametrize("raw,expected", [
        ("Eyal", "Eyal Zror"),
        ("eyal", "Eyal Zror"),
        ("EYAL", "Eyal Zror"),
        ("Eyal Zror", "Eyal Zror"),
        ("  Paolo  ", "Paolo Vailetti"),
        ("roye", "Roye Tadmor"),
        ("Yoram", "Yoram Weiss"),
        ("Nechama", "Nechama Tik"),
        ("nechama tik", "Nechama Tik"),
    ])
    def test_first_names_resolve_to_full_names(self, sc, raw, expected):
        assert sc.resolve_assignee(raw, roster=ROSTER) == expected

    def test_blank_stays_blank(self, sc):
        # An unassigned task is a legitimate state, not a defect —
        # get_tasks_without_assignee surfaces it for gap-fill.
        assert sc.resolve_assignee("", roster=ROSTER) == ""
        assert sc.resolve_assignee(None, roster=ROSTER) == ""
        assert sc.resolve_assignee("   ", roster=ROSTER) == ""

    @pytest.mark.parametrize("group", [
        "CropSight team", "CropSight technical team", "team", "everyone", "TBD",
    ])
    def test_group_buckets_are_never_guessed_into_a_person(self, sc, group):
        assert sc.resolve_assignee(group, roster=ROSTER) == group

    def test_unknown_name_is_returned_as_is(self, sc):
        """Sheets-wins: never destroy what a human typed."""
        assert sc.resolve_assignee("Debra Nachlis", roster=ROSTER) == "Debra Nachlis"
        assert sc.resolve_assignee("Shemer Topper", roster=ROSTER) == "Shemer Topper"

    @pytest.mark.parametrize("raw,expected", [
        ("Paolo, Eyal", "Paolo Vailetti"),
        ("Eyal Zror, Roye Tadmor", "Eyal Zror"),
        ("Roye and Paolo", "Roye Tadmor"),
    ])
    def test_multi_owner_takes_primary_never_splits(self, sc, raw, expected):
        """Eyal's call (2026-07-22): primary owner in the field, others in the
        title. Splitting would change task counts and history."""
        assert sc.resolve_assignee(raw, roster=ROSTER) == expected

    def test_multi_owner_with_unknown_primary_kept_verbatim(self, sc):
        assert sc.resolve_assignee("Someone Else, Eyal", roster=ROSTER) == "Someone Else, Eyal"

    def test_roster_failure_keeps_the_name(self, sc, monkeypatch):
        """A DB blip must never blank or mangle an assignee."""
        def _boom():
            raise RuntimeError("supabase down")
        monkeypatch.setattr(sc, "list_team_members", _boom)
        assert sc.resolve_assignee("Eyal") == "Eyal"

    def test_first_name_collision_prefers_exact_full_name(self, sc):
        roster = [{"name": "Eyal Zror"}, {"name": "Eyal Cohen"}]
        # exact full name always wins over the ambiguous first-name scan
        assert sc.resolve_assignee("Eyal Cohen", roster=roster) == "Eyal Cohen"


class TestHonorificsAndSurnames:
    """The live roster stores Yoram as "Prof. Yoram Weiss". Without honorific
    stripping, NEITHER "Yoram" nor "Yoram Weiss" matched it — the backfill dry
    run silently left 21 rows unnormalized until this was caught. [2026-07-22]"""

    ROSTER = [
        {"name": "Eyal Zror"}, {"name": "Roye Tadmor"}, {"name": "Paolo Vailetti"},
        {"name": "Prof. Yoram Weiss"}, {"name": "Hadar"}, {"name": "Ido"},
    ]

    @pytest.mark.parametrize("raw", ["Yoram", "Yoram Weiss", "yoram weiss",
                                     "Prof. Yoram Weiss", "prof yoram weiss"])
    def test_titled_roster_name_matches_every_spelling(self, sc, raw):
        assert sc.resolve_assignee(raw, roster=self.ROSTER) == "Prof. Yoram Weiss"

    def test_unambiguous_surname_resolves(self, sc):
        assert sc.resolve_assignee("Tadmor", roster=self.ROSTER) == "Roye Tadmor"

    def test_ambiguous_surname_is_left_alone(self, sc):
        """Two roster members sharing a surname must never be guessed between."""
        roster = [{"name": "Eyal Zror"}, {"name": "Dana Zror"}]
        assert sc.resolve_assignee("Zror", roster=roster) == "Zror"

    @pytest.mark.parametrize("single", ["Hadar", "Ido"])
    def test_single_word_roster_names_still_work(self, sc, single):
        assert sc.resolve_assignee(single, roster=self.ROSTER) == single


class TestResolveStatus:
    """Status was written RAW at every boundary, so a sheet cell 'Done'
    persisted and get_tasks(status='done') missed it — 7 live rows, 2026-07-23."""

    @pytest.mark.parametrize("raw,expected", [
        ("Done", "done"), ("done", "done"), ("DONE", "done"),
        ("Pending", "pending"), ("Overdue", "overdue"), ("Archived", "archived"),
        ("In Progress", "in_progress"), ("in-progress", "in_progress"),
        ("in_progress", "in_progress"),
    ])
    def test_normalizes_case_and_separators(self, sc, raw, expected):
        assert sc.resolve_status(raw) == expected

    def test_none_and_blank_pass_through(self, sc):
        assert sc.resolve_status(None) is None
        assert sc.resolve_status("") == ""

    def test_unknown_kept_verbatim(self, sc):
        assert sc.resolve_status("blocked") == "blocked"
