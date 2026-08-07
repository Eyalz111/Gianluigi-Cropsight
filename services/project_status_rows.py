"""
Pure row/column model for the Project Status sheet.

No I/O. Everything here is a pure function over the raw `values` grid Sheets
returns, so the whole layout contract is unit-testable without a spreadsheet.
Analogue of `services/gantt_rows.py`, which plays the same role for the Gantt.

LAYOUT — a project row, then its action rows, then the next project:

    A #  B Subject  C Action  D To do  E Date  F Resp.  G Comments │ H..L hidden
    1    Kenya      (blank)   <objective>  12/08  Paolo   <notes>  │ P  p-a1f  p-a1f
    [ ]             Chase NCPB…            15/08  Paolo            │ A  t-9c2  p-a1f
    2    Italy      …                                              │ P  p-e3d  p-e3d

Two rules the rest of the engine leans on:

1. **Identity is per-row, never positional.** Every system row carries its own
   uid AND its parent's, so a row can be inserted, dragged or re-ordered
   without the engine losing track. A HUMAN row has no uid yet — its parent is
   the nearest project row above it.

2. **Column positions are resolved from the header text at read time**, not
   hard-coded letters, so inserting a column doesn't silently shift the map.
   Same instinct as gantt_rows.resolve_row_by_topic rescanning rather than
   trusting a stored row number.

Column A does double duty: the `#` for project rows, a done CHECKBOX for action
rows. That is what keeps the sheet at the 7 visible columns of the template.
"""

import hashlib
import re
from dataclasses import dataclass, field

# Visible columns — the template, unchanged.
COL_NUM = "#"
COL_SUBJECT = "Subject"
COL_ACTION = "Action"
COL_TODO = "To do"
COL_DATE = "Date"
COL_RESP = "Resp."
COL_COMMENTS = "Comments"

VISIBLE_HEADERS = [COL_NUM, COL_SUBJECT, COL_ACTION, COL_TODO,
                   COL_DATE, COL_RESP, COL_COMMENTS]

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
        return re.sub(r"[^a-z0-9]", "", str(s or "").lower())

    found = {}
    for idx, cell in enumerate(header_row or []):
        key = norm(cell)
        for name in ALL_HEADERS:
            if key and key == norm(name):
                found.setdefault(name, idx)
    for default_idx, name in enumerate(ALL_HEADERS):
        found.setdefault(name, default_idx)
    return found


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
    subject = cell(raw, cols, COL_SUBJECT)
    todo = cell(raw, cols, COL_TODO)
    date = cell(raw, cols, COL_DATE)
    resp = cell(raw, cols, COL_RESP)
    comments = cell(raw, cols, COL_COMMENTS)

    if action:
        return HUMAN_ACTION
    if subject or todo:
        return HUMAN_PROJECT
    if date or resp or comments:
        # She started a row and stopped. Counted and surfaced, never written —
        # a task with no title is worse than no task.
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
    """Stable hash of the tab's uid sequence.

    Taken before the value pass and re-checked immediately before the
    structural pass: if it changed, a human edited the tab mid-cycle and the
    row numbers we computed are stale, so that tab's structural work is skipped
    rather than applied at the wrong offsets.
    """
    seq = "|".join(f"{r.kind}:{r.uid}" for r in iter_rows(blocks, orphans))
    return hashlib.sha1(seq.encode("utf-8")).hexdigest()
