"""
Pure row/column model for the Project Status sheet.

No I/O. Everything here is a pure function over the raw `values` grid Sheets
returns, so the whole layout contract is unit-testable without a spreadsheet.
Analogue of `services/gantt_rows.py`, which plays the same role for the Gantt.

LAYOUT — a project row, then its action rows, then the next project:

    A #  B Project  C Topic  D Action  E To do  F Date  G Resp.  H Priority  I Comments │ hidden
    1    Kenya                          <objective>  12/08  Paolo                <notes>  │ P p-a1f
    [ ]           NCPB      Chase…                   15/08  Paolo   H                     │ A t-9c2
    2    Italy                          …                                                 │ P p-e3d

Two rules the rest of the engine leans on:

1. **Identity is per-row, never positional.** Every system row carries its own
   uid AND its parent's, so a row can be inserted, dragged or re-ordered
   without the engine losing track. A HUMAN row has no uid yet — its parent is
   the nearest project row above it.

2. **Column positions are resolved from the header text at read time**, not
   hard-coded letters, so inserting a column doesn't silently shift the map.
   Same instinct as gantt_rows.resolve_row_by_topic rescanning rather than
   trusting a stored row number.

3. **The kind of a row is DECLARED.** `Project` filled -> a project. `Action`
   filled -> an action. Both filled is AMBIGUOUS and is reported, never
   guessed. Until 2026-08-09 this was inferred from whether the cell beside
   `Subject` happened to be blank.

Column A does double duty: the `#` for project rows, a done CHECKBOX for action
rows.
"""

import hashlib
import re
from dataclasses import dataclass, field

# Visible columns — the template, unchanged.
COL_NUM = "#"
# THE KIND OF A ROW IS DECLARED, NOT INFERRED. Until 2026-08-09 `Subject` meant
# the project name when `Action` was empty and the topic when it was not — the
# same column creating a project or tagging a task depending on whether the cell
# beside it happened to be blank. Eyal hit it: "AWS expected expenses" became a
# project when he meant an action, and his verdict was "i prefer solve those
# stuff well in advance and not go with such a not robust solution".
#
# A dedicated column makes it structural. `Project` filled -> project row.
# `Action` filled -> action row. Nothing to infer, and a row with both is an
# error we can report rather than a guess we have to make.
COL_PROJECT = "Project"
# Renamed from `Subject`: it already meant the topic on action rows, and
# "Subject" sitting beside "Project" reads as a synonym.
COL_TOPIC = "Topic"
COL_ACTION = "Action"
COL_TODO = "To do"
COL_DATE = "Date"
COL_RESP = "Resp."
# Brought over from the Tasks tab — one of the two reasons that tab still had a
# reason to exist. 'Urgent' is new; L/M/H already exist on tasks.priority.
COL_PRIORITY = "Priority"
COL_COMMENTS = "Comments"

VISIBLE_HEADERS = [COL_NUM, COL_PROJECT, COL_TOPIC, COL_ACTION, COL_TODO,
                   COL_DATE, COL_RESP, COL_PRIORITY, COL_COMMENTS]

PRIORITIES = ("Urgent", "H", "M", "L")
# One colour per level, so the column can be read at a glance instead of
# letter by letter. Only Urgent was coloured, which made the other three
# indistinguishable and the column mostly decorative. A heat scale: red, orange,
# neutral sand, cool grey-blue — deliberately the same four on the meetings tab,
# because one scale meaning one thing everywhere is the point of sharing it.
PRIORITY_COLORS = {
    "Urgent": "#F4CCCC",
    "H": "#FCE4D6",
    "M": "#FFF2CC",
    "L": "#DEEAF6",
}
# Sheet value -> tasks.priority. Stored uppercase-single-letter as today, with
# 'U' for urgent so the existing H/M/L ordering logic keeps working.
PRIORITY_TO_DB = {"urgent": "U", "u": "U", "h": "H", "high": "H",
                  "m": "M", "medium": "M", "l": "L", "low": "L"}
PRIORITY_TO_SHEET = {"U": "Urgent", "H": "H", "M": "M", "L": "L"}

# Hidden, system-owned. Named with a leading underscore so they read as
# machinery if anyone unhides them.
COL_KIND = "_kind"        # 'P' | 'A'
COL_UID = "_uid"          # canonical_projects.id (P) | tasks.id (A)
COL_PARENT = "_parent"    # owning canonical_projects.id
COL_ORIGIN = "_origin"    # 'auto' | 'manual' | 'auto_edited'
COL_SRC = "_src"          # meeting_id for auto rows

HIDDEN_HEADERS = [COL_KIND, COL_UID, COL_PARENT, COL_ORIGIN, COL_SRC]
ALL_HEADERS = VISIBLE_HEADERS + HIDDEN_HEADERS

KIND_PROJECT = "P"
KIND_ACTION = "A"

ORIGIN_AUTO = "auto"
ORIGIN_MANUAL = "manual"
ORIGIN_AUTO_EDITED = "auto_edited"

# Row classifications
SYSTEM_PROJECT = "system_project"
SYSTEM_ACTION = "system_action"
HUMAN_ACTION = "human_action"
HUMAN_PROJECT = "human_project"
INCOMPLETE = "incomplete"
# Project AND Action both filled — the one case the declared rule cannot
# settle. Reported, never guessed.
AMBIGUOUS = "ambiguous"
SPACER = "spacer"

# The header block occupies rows 1-3 (title / distribution / headers); body
# starts at row 4. Matches the supplied template's freeze at A4.
HEADER_ROW = 3
FIRST_BODY_ROW = 4


def resolve_columns(header_row: list) -> dict:
    """{header name -> 0-based index} from the sheet's own header row.

    Falls back to the canonical order for any header not found, so a partially
    renamed sheet degrades to the default position instead of raising. Matching
    is case/space-insensitive because "Resp." and "Resp" both occur in the wild.
    """
    def norm(s) -> str:
        text = str(s or "").strip().lower()
        stripped = re.sub(r"[^a-z0-9]", "", text)
        # "#" is entirely punctuation, so stripping non-alphanumerics reduced it
        # to "" — which the truthiness guard below then skipped, meaning column A
        # could NEVER be resolved from the sheet and always fell back to its
        # hard-coded index. Insert a column to the left and every action row's
        # checkbox is read from the wrong cell: ticked work silently reverts to
        # pending. Fall back to the raw text when normalising empties it.
        # [2026-08-08 code review]
        return stripped or text

    found = {}
    for idx, cell in enumerate(header_row or []):
        key = norm(cell)
        for name in ALL_HEADERS:
            if key and key == norm(name):
                found.setdefault(name, idx)
    for default_idx, name in enumerate(ALL_HEADERS):
        found.setdefault(name, default_idx)
    return found


def unresolved_columns(header_row: list) -> list:
    """Headers the sheet does NOT declare — the layout-mismatch guard.

    `resolve_columns` falls back to the canonical position for anything it
    cannot find, which is the right behaviour for ONE renamed header and
    catastrophic for a WHOLE DIFFERENT LAYOUT. Reading the 12-column sheet with
    the 14-column model resolves `Topic` to the old `Action` column and
    `Priority` to the old `_kind` — so the engine would compare an action's text
    against a task's label and try to pull "A" in as a priority, on every row.

    So the caller checks first: if a tab does not declare the layout this build
    expects, it is skipped entirely and says so. That covers the deploy window
    of this migration and, permanently, anyone who renames a header by hand.
    """
    def norm(s) -> str:
        text = str(s or "").strip().lower()
        stripped = re.sub(r"[^a-z0-9]", "", text)
        return stripped or text

    present = {norm(c) for c in (header_row or []) if norm(c)}
    return [name for name in ALL_HEADERS if norm(name) not in present]


def cell(row: list, cols: dict, name: str) -> str:
    """Value at a named column, '' when the row is short or the cell is blank."""
    idx = cols.get(name)
    if idx is None or idx >= len(row or []):
        return ""
    value = row[idx]
    return "" if value is None else str(value).strip()


def is_checked(value) -> bool:
    """Google Sheets checkbox truthiness.

    The values API returns booleans for checkbox cells but strings ('TRUE') for
    plain text that looks like one, and locale-translated forms appear when a
    human types instead of ticking. Accept all of them.
    """
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in ("true", "yes", "y", "x", "✓", "v", "done")


# ── provenance ───────────────────────────────────────────────────────────────

_PROV_RE = re.compile(
    r"\[\s*(auto|manual)\s*(?:·[^\]]*)?\]", re.IGNORECASE
)


def format_provenance(origin: str, source: str = "", when: str = "",
                      closed: str = "") -> str:
    """The visible marker appended to a SYSTEM row's Action cell.

    Only ever written into a cell the system owns. A human row carries no
    marker at all — that absence is the signal, and it is what lets us mark
    provenance without ever writing into her text.
    """
    if origin == ORIGIN_MANUAL:
        return "[manual · Nechama]"
    bits = ["auto"]
    if source:
        bits.append(str(source)[:45])
    if when:
        bits.append(when)
    if closed:
        bits.append(f"done {closed}")
    return "[" + " · ".join(bits) + "]"


def _plain_date(value) -> str:
    """Default date renderer — blank for empty, never the string 'None'."""
    return "" if value in (None, "", "None") else str(value)


def project_row_values(project: dict, num="", fmt_date=_plain_date) -> list:
    """A project row in ALL_HEADERS order.

    THE ONE PLACE A ROW IS SHAPED. Three call sites used to hand-build this
    literal — the pack builder, the injection path and the missing-block path —
    and they drifted: the builder left `Subject` empty while the database held a
    label, so the merge saw a divergence and queued 37 phantom cell writes on a
    sheet nobody had touched. Adding a column made that a certainty rather than a
    risk, so the literal now lives here, beside the header list it has to match.
    """
    pid = project.get("id") or ""
    return [
        num,                                  # '#' — set by the structural pass
        project.get("name") or "",            # the declaring cell
        "",                                   # Topic: action rows only
        "",                                   # Action: action rows only
        project.get("objective") or "",
        fmt_date(project.get("target_date")),
        project.get("owner") or "",
        "",                                   # Priority: action rows only
        project.get("notes") or "",
        KIND_PROJECT, pid, pid, "", "",
    ]


def action_row_values(task: dict, parent_uid: str = "", marker: str = "",
                      fmt_date=_plain_date, checked: bool = False) -> list:
    """An action row in ALL_HEADERS order. See project_row_values."""
    title = (task.get("title") or "").strip()
    return [
        checked,                              # column A is the done checkbox
        "",                                   # Project: project rows only
        task.get("label") or "",
        f"{title} {marker}".strip() if marker else title,
        "",                                   # To do belongs to the project row
        fmt_date(task.get("deadline")),
        task.get("assignee") or "",
        # Blank when the task carries no priority, NOT a default 'M'. Showing a
        # value the database does not hold makes the merge see a divergence and
        # push it back every cycle, and it tells the reader the work has been
        # triaged when nobody has triaged it.
        PRIORITY_TO_SHEET.get(str(task.get("priority") or "").strip().upper(), ""),
        task.get("notes") or "",
        KIND_ACTION, task.get("id") or "", parent_uid or "",
        task.get("_origin") or ORIGIN_AUTO, task.get("meeting_id") or "",
    ]


def parse_provenance(text: str) -> str | None:
    """'auto' | 'manual' | None. Display sanity only — machine truth is _origin."""
    m = _PROV_RE.search(text or "")
    return m.group(1).lower() if m else None


def strip_provenance(text: str) -> str:
    """The action text without its marker, for comparing against the DB title."""
    return _PROV_RE.sub("", text or "").strip()


# ── rows and blocks ──────────────────────────────────────────────────────────

@dataclass
class Row:
    """One parsed sheet row, with its 1-based sheet row number."""
    row_number: int
    kind: str                    # classification, not the raw _kind cell
    values: dict = field(default_factory=dict)   # visible column -> str
    uid: str = ""
    parent: str = ""
    origin: str = ""
    src: str = ""
    checked: bool = False

    @property
    def is_system(self) -> bool:
        return self.kind in (SYSTEM_PROJECT, SYSTEM_ACTION)

    @property
    def is_human(self) -> bool:
        return self.kind in (HUMAN_ACTION, HUMAN_PROJECT)


@dataclass
class Block:
    """A project row and the action rows beneath it."""
    project: Row | None
    actions: list = field(default_factory=list)

    @property
    def project_uid(self) -> str:
        return self.project.uid if self.project else ""

    @property
    def end_row(self) -> int:
        """Sheet row after the last row of this block — the insert anchor."""
        rows = ([self.project] if self.project else []) + list(self.actions)
        return (max(r.row_number for r in rows) + 1) if rows else FIRST_BODY_ROW


def classify_row(raw: list, cols: dict) -> str:
    """What a row IS, from its cells alone.

    A system row declares itself via `_kind`. A human row has no `_kind` — it is
    classified purely by which cells she filled, because that is the only signal
    available and it must never depend on where the row sits.
    """
    kind = cell(raw, cols, COL_KIND).upper()
    if kind == KIND_PROJECT:
        return SYSTEM_PROJECT
    if kind == KIND_ACTION:
        return SYSTEM_ACTION

    action = cell(raw, cols, COL_ACTION)
    project = cell(raw, cols, COL_PROJECT)
    todo = cell(raw, cols, COL_TODO)
    date = cell(raw, cols, COL_DATE)
    resp = cell(raw, cols, COL_RESP)
    comments = cell(raw, cols, COL_COMMENTS)
    priority = cell(raw, cols, COL_PRIORITY)

    # DECLARED, NOT INFERRED. `Project` filled means a project; `Action` filled
    # means an action. The old rule keyed on Subject-with-an-empty-Action, so
    # the same column created a project or tagged a task depending on whether
    # the cell beside it happened to be blank — which is how "AWS expected
    # expenses" became a project when Eyal meant an action. [2026-08-09]
    if project and action:
        # Genuinely ambiguous, so it is reported rather than guessed. Guessing
        # here is exactly the behaviour this change exists to remove.
        return AMBIGUOUS
    if action:
        return HUMAN_ACTION
    if project:
        return HUMAN_PROJECT
    if todo or date or resp or comments or priority:
        # She started a row and stopped. Counted and surfaced, never written —
        # a task with no title is worse than no task. `To do` counts as started
        # but not finished: without a Project it names no project, and without
        # an Action it is not a step.
        return INCOMPLETE
    return SPACER


def parse_row(raw: list, cols: dict, row_number: int) -> Row:
    """Build a Row from a raw sheet row."""
    kind = classify_row(raw, cols)
    values = {name: cell(raw, cols, name) for name in VISIBLE_HEADERS}
    num_idx = cols.get(COL_NUM, 0)
    raw_num = raw[num_idx] if num_idx < len(raw or []) else ""
    return Row(
        row_number=row_number,
        kind=kind,
        values=values,
        uid=cell(raw, cols, COL_UID),
        parent=cell(raw, cols, COL_PARENT),
        origin=cell(raw, cols, COL_ORIGIN).lower(),
        src=cell(raw, cols, COL_SRC),
        # Only action rows use column A as a checkbox; on a project row it is
        # the '#' and a numeral must never read as "done".
        checked=is_checked(raw_num) if kind in (SYSTEM_ACTION, HUMAN_ACTION) else False,
    )


def parse_tab(values: list, first_body_row: int = FIRST_BODY_ROW) -> tuple:
    """Parse a tab's raw grid into (blocks, orphans, columns).

    `orphans` are action rows appearing before any project row — they are not
    dropped, they get attached to the area's "Others" project by the engine.
    Losing a row she typed because it sat in the wrong place would be the worst
    possible behaviour.
    """
    grid = values or []
    header = grid[HEADER_ROW - 1] if len(grid) >= HEADER_ROW else []
    cols = resolve_columns(header)

    blocks: list = []
    orphans: list = []
    current: Block | None = None

    for offset, raw in enumerate(grid[first_body_row - 1:]):
        row_number = first_body_row + offset
        row = parse_row(raw, cols, row_number)
        if row.kind == SPACER:
            continue
        if row.kind in (SYSTEM_PROJECT, HUMAN_PROJECT):
            current = Block(project=row)
            blocks.append(current)
            continue
        if current is None:
            orphans.append(row)
        else:
            current.actions.append(row)
    return blocks, orphans, cols


def find_duplicate_uids(blocks: list, orphans: list = None) -> dict:
    """{uid: [Row, ...]} for any uid appearing more than once.

    A pasted block carries its source's uids. The topmost occurrence keeps the
    identity; the rest have their SYSTEM columns cleared (never her text) so
    they become new items on the next pass.
    """
    seen: dict = {}
    for row in iter_rows(blocks, orphans):
        if row.uid:
            seen.setdefault(row.uid, []).append(row)
    return {uid: rows for uid, rows in seen.items() if len(rows) > 1}


def iter_rows(blocks: list, orphans: list = None):
    """Every parsed row, project and action, in sheet order."""
    for block in blocks or []:
        if block.project:
            yield block.project
        for action in block.actions:
            yield action
    for row in orphans or []:
        yield row


def tab_fingerprint(blocks: list, orphans: list = None) -> str:
    """Stable hash of the tab's uid sequence AND their row positions.

    Taken before the value pass and re-checked immediately before the
    structural pass: if it changed, a human edited the tab mid-cycle and the
    row numbers we computed are stale, so that tab's structural work is skipped
    rather than applied at the wrong offsets.

    ROW NUMBERS ARE PART OF THE HASH. Hashing only the uid sequence made the
    guard blind to the single most likely mid-review edit: inserting a blank
    row. parse_tab discards blank rows entirely, so the sequence was unchanged
    while every row BELOW the insert had shifted by one — the guard passed and
    a queued deleteDimension landed on the wrong line, silently removing a live
    row. Position is exactly what the structural pass depends on, so position
    is what has to be verified. Text edits still don't move rows, so retyping
    an action still doesn't trip it. [2026-08-08 code review]
    """
    seq = "|".join(f"{r.kind}:{r.uid}@{r.row_number}"
                   for r in iter_rows(blocks, orphans))
    return hashlib.sha1(seq.encode("utf-8")).hexdigest()
