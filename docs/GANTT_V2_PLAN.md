# Gantt v2 — connecting the timeline to the Project Status file

**Status: PLAN, not built.** Written 2026-08-12 after reading the live board,
the code surface, and the database. Every number below was measured, not
assumed; the probes are reproducible.

Eyal's framing: *"our current gantt is complex and detached from everything…
change it from 'planning and execution' rows of each area to the actual
projects we work on… it is important for me to have actual dates in the gantt
and not just sequences."*

---

## 1. What the current Gantt actually is

`Cropsight operational gantt` — 6 tabs; the live one is `2026-2027`, **1018
rows × 101 columns**.

It is **not** a date-range Gantt. It is a **weekly-column board**:
`Section | Sub-category | Own | Due |` then ~97 week columns (`W9 02/03`,
`W10 09/03`, …). A "bar" is the same text repeated across consecutive week
cells.

**Eight sections.** Six match the Project Status areas *exactly* — Product &
Technology, Sales & BD, Client Delivery & Operations, Fundraising & Investor
Relations, Legal Corporate & Finance, Team & HR — plus `STRATEGIC MILESTONE`
and `MANAGEMENT — CEO OP` on top.

Each area has **fixed lanes**: Planning #1–2, Execution #1–3, Meetings, Human
Resources (+ Marketing in Sales, Finance & Admin in Legal).

### Why "detached" is literally true

| Field | State |
|---|---|
| `Own` column | **empty on every row** |
| `Due` column | **empty on every row** |
| Ownership | encoded in text prefixes — `[R/E]`, `[E/P]`, `[R]` |
| Timing | encoded in *which week column* the text sits in |
| `gantt_rows.topic_id` | **0 of 82 populated** |
| `gantt_rows.area_id` | 79 of 82 populated |

There is nothing structured to join on. The board knows its *area* and nothing
about which *project* a bar belongs to.

### It is, however, fully recoverable

`scratchpad/gantt_extract.py` parses the week headers into real dates and walks
each row grouping consecutive same-text cells into bars:

- **397 bars recovered**, **174 real** after dropping meeting-count noise
- **145 area work bars**, spanning **2026-03-02 → 2027-12-27**
- Milestones already marked with **★** (e.g. `★ MVP Product Delivery (Q3 2026)`)

So "learn from the previous Gantt" is not aspirational — the timelines are
already extractable as actual dates. That script becomes Phase 0.

### Live defects in the current board

- `All Meetings (Aggregate)` row shows **`#REF!`** across several weeks
- `STRATEGIC MILESTONE` section is **structurally present but completely
  empty** — no bars at all. The real milestones live under `Company OKRs`.

---

## 2. Where the milestones actually are

Not in the milestone section. Under `MANAGEMENT — CEO OP → Company OKRs`:

```
Q1 OKR: Finalize BP — Legal entity Establishment   2026-03-30
"Investor's Package" readiness                     2026-04-20
Signing #1 MVP client                              2026-06-01
Signing #1 MVP client — postponed here             2026-07-06   <- slipped
```

Two things follow. First, these are exactly the KPIs Eyal named (raising
funds, MVP delivery, first client). Second, **the board carries slippage
history** — the same milestone appears twice, with the second annotated
"postponed here". A milestone model that cannot express "this moved, and when"
would lose information the current board already holds.

`Strategy & Decisions` also carries a recurring cadence — *Monthly Investor &
Strategic Stakeholder Update* every month through 2027-11, and *Annual
Strategic Planning* each December. That is the same recurrence concept as the
Meetings pool (#4), and should reuse it rather than invent a second one.

---

## 3. The start-date problem, and the answer

**Every date in the database is an END.** `canonical_projects.target_date`
(12 of 23 filled), `tasks.deadline`, `follow_up_meetings.proposed_date`. There
is no start column anywhere.

Eyal's model: *start = when the project (or task) first appeared, or set by
hand.* Measured against real data:

| Source | Spread | Verdict |
|---|---|---|
| `canonical_projects.created_at` | 23 projects across **3 days** (15 on 2026-08-07) | ✗ that is the v2 rollout date, not when work began |
| earliest task's `created_at` | **14 distinct days** across 22 projects | ✓ plausible and usable |
| `tasks.created_at` (waterfall) | 86 open tasks across **29 days** | ✓ good |
| old Gantt bar start | 16 of 23 matched by name; only **4 better** | ⚠ unreliable alone |

So the model is right, applied one level down: **project start = earliest
task's `created_at`, not the project row's.**

### Why the old-Gantt seed must be human-confirmed

Naive name matching produces confident nonsense:

```
Corporate               -> "Monthly close — send docs to Shimony"  2027-05-31  WRONG
Fundraising & Investors -> "Post-Round Lessons"                    2026-07-27  WRONG
Product V1, MVP Delivery, Others—Client Delivery, Others—P&T
                        -> all four matched the SAME bar                       WRONG
```

But where it *is* right it is genuinely better than the task-derived date,
because the board records when planning began rather than when a transcript
first mentioned the work:

```
Legal              first task 2026-04-09   gantt "Legal entity Establishment"  2026-03-02
Marketing & Brand  first task 2026-03-26   gantt "Marketing Strategy Kick Off" 2026-03-09
Investor Outreach  first task 2026-07-05   gantt "close circle outreach"       2026-04-13
```

**Therefore: propose, never auto-apply.** This is the existing approve gate,
not a new mechanism.

### The three-tier rule

```
1. manual_start_date = TRUE      -> use start_date            (human wins, always)
2. else if a frozen start exists -> use it                    (stable)
3. else                          -> earliest task created_at,
                                    then FREEZE it            (compute once)
```

**Freezing matters.** "Earliest task" moves backwards whenever an older task is
attached to a project later — the bar would silently redraw and read as a plan
change. Compute once, store, then it is frozen-or-manual. Same sticky-rail
shape as every other editable field in this system.

Two projects (`Others — Client Delivery`, `Others — Product & Technology`) are
catch-all buckets genuinely created at rollout; they need manual dates. One
project has no tasks at all.

---

## 4. Data model

### Migration A — project start dates

```sql
ALTER TABLE canonical_projects
    ADD COLUMN IF NOT EXISTS start_date        DATE,
    ADD COLUMN IF NOT EXISTS manual_start_date BOOLEAN DEFAULT FALSE;
```

**No default on `start_date`.** A `DEFAULT` backfills every row and the
reconcile then announces a decision nobody made — that is exactly what
`priority TEXT DEFAULT 'M'` did to 122 meetings on 2026-08-09.

`manual_start_date` joins the `_PROJECT_MANUAL_FIELDS` tuple so Rule 2 defends
a human-set start.

### Migration B — milestones

Milestones are not projects (no owner, no task tree, no area necessarily) and
not tasks (they are outcomes, not actions). They need their own table, and it
must express slippage:

```sql
CREATE TABLE IF NOT EXISTS milestones (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title           TEXT NOT NULL,
    target_date     DATE,
    original_date   DATE,          -- first committed date; never overwritten
    status          TEXT NOT NULL DEFAULT 'open',   -- open|hit|missed|dropped
    kind            TEXT,          -- funding|product|commercial|corporate
    area_id         UUID REFERENCES areas(id),
    project_id      UUID REFERENCES canonical_projects(id),
    notes           TEXT,
    manual_target_date BOOLEAN DEFAULT FALSE,
    manual_status      BOOLEAN DEFAULT FALSE,
    manual_set_at      TIMESTAMPTZ,
    manual_set_source  TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE milestones ENABLE ROW LEVEL SECURITY;   -- MANDATORY, see CLAUDE.md
```

`original_date` is what makes *"Signing #1 MVP client — postponed here"*
expressible: the row keeps its first commitment and shows the slip, instead of
quietly moving.

### Migration C — the extraction archive

The old board is the only record of 174 dated bars. Before anything changes,
persist it:

```sql
CREATE TABLE IF NOT EXISTS gantt_legacy_bars (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    section      TEXT, lane TEXT, label TEXT,
    start_date   DATE, end_date DATE, weeks INT,
    source_tab   TEXT, extracted_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE gantt_legacy_bars ENABLE ROW LEVEL SECURITY;
```

This is deliberately a **dumb archive** — no foreign keys, no interpretation.
It is the evidence base for seeding and for any later "what did we plan in
March?" question, and it survives whatever happens to the sheet.

---

## 5. Surfaces

### `Timeline` tab — in the Project Status workbook

One row per project, grouped by area (the six existing areas), with the
waterfall as native Sheets **row grouping** (collapse/expand) rather than a
second tab:

```
▸ PRODUCT & TECHNOLOGY
    Cloud Infrastructure       Roye    ████████░░░░   22 Jul → 15 Sep   H
  ▾ CropSight Accuracy Model   Matti   ░░████████░░   13 Jun → 30 Aug   Urgent
      └ Check Bedrock availability        ██
      └ Research Parquet format             ███
```

- **Priority colours** — the Urgent/H/M/L scale already on the sheet
- A **today marker** column
- Dynamic rows, so the old 5-lane cap disappears (Fundraising and Sales both
  have 6 projects and overflow it today)

#### The grid *(decided 2026-08-12)*

**Weekly columns, one tab, 2026-03-02 → 2027-12-27 — about 96 columns.**

No yearly tabs. Eyal chose weeks over months for resolution, and end-2027 over
end-2028 to keep the width workable — 145 weekly columns to end-2028 would be
mostly horizontal scrolling.

That span is **exactly what the current board already covers**, which is a
useful accident: the legacy bars extracted in Phase 0 land on the *same grid*,
so "what we planned in March" can be overlaid against "what we think now"
column-for-column, with no date arithmetic. Worth building the extract with
that overlay in mind even if it ships later.

#### Bar ends *(decided 2026-08-12)*

A project with no `target_date` renders **open-ended to the right** — the bar
runs to the edge with no terminator. Eyal closes it by entering a date when he
knows one.

Deliberately *not* falling back to the latest open task deadline: that end
would move every time a task was added or re-dated, so a bar would appear to
change plan when nothing was decided. An open end is honest about not knowing;
a derived end is a guess wearing a date. 11 of 23 projects have no target
today.

### `CEO` tab — milestones and management

Eyal: *"maybe we should have another 'managment' or ceo tab."* Yes — and it
should be a **separate tab, not a band on the Timeline**, because its rows are
a different kind of object with a different edit cadence.

```
MILESTONES
  ★ Signing #1 MVP client        commercial   1 Jun → 6 Jul   SLIPPED 5w
  ★ MVP Product Delivery         product      31 Aug          open
  ★ Investor's Package readiness funding      20 Apr          hit

MANAGEMENT  (hand-maintained — no DB source exists)
  Company OKRs · Strategy & Decisions · Escalations · Availability
```

The management block stays **manual**, as Eyal expected. Nothing in the
database corresponds to OKRs, escalations or availability, and inventing a
source would be worse than a hand-maintained block that is honest about being
hand-maintained. The milestone block above it *is* DB-backed and editable both
ways.

### Bidirectional editing *(decided 2026-08-12)*

Eyal: *"if I change in the new gantt something like responsible or due dates, I
will want to see it in the sheet of the project status (and the DB of
course)."* Yes — **at project level. Task rows stay read-only on the Timeline.**

The reason is recent and specific. On 2026-08-08 this system deliberately
collapsed to **one editable surface for tasks** (`TASKS_TAB_READ_ONLY=true`),
because *"two writers on the same rows produced every cross-surface defect of
2026-08: the rename-revert loop, the per-task manual_set_at recency bug, and
three labels left permanently divergent because the Tasks tab pulled a stale
cell over a value Project Status had just written."*

A Timeline whose waterfall rows are editable puts a second writer on
`tasks.deadline` and `tasks.assignee` — the exact fields Nechama edits daily on
the area tabs. Project rows carry no such collision:

| Field | Editable today | On the Timeline |
|---|---|---|
| `start_date` | nowhere — the column does not exist yet | **its only home** |
| `canonical_projects.target_date` | Projects tab (rarely visited) | **editable** |
| `canonical_projects.owner` | Projects tab | **editable** |
| `tasks.deadline` / `tasks.assignee` | area tabs, daily | **read-only** |

So Eyal gets what he asked for — change the responsible or the due date on the
Gantt, see it in Project Status and the database — without a second writer on
the rows that produced every cross-surface defect this month.

**Mechanism already exists.** `sheet_snapshots` supports one merge base per
SURFACE: `uq_sheet_snapshots_task` and `uq_sheet_snapshots_ps_action` are
partial indexes on the same `task_id` and coexist precisely so an edit on one
surface does not read as divergence on the other. The Timeline takes
`entity_type='gantt_project'`.

**Conflict rule: report, never guess.** If the same field changed on both the
Timeline and the Projects tab within one cycle, the reconcile writes neither
and surfaces it — the same instinct as an `AMBIGUOUS` row, where `Project` and
`Action` both filled is reported rather than resolved. Two humans disagreeing
is not a merge problem, it is a question.

If task-level editing on the Timeline is wanted later, the honest path is to
make the Timeline the *only* task-date surface — which means taking it off the
area tabs. That is a different decision, not an increment of this one.

### Registration — non-negotiable

Both tabs go in `NON_AREA_TABS`, **imported** from their module rather than
spelled again. Without it the reconcile parses them as area tabs and the
formatting pass strips them — precisely what happened to the meetings pool
(defect #9, 2026-08-09), and what nearly happened to the Focus tab.

---

## 6. Phases

Each phase is independently shippable, flag-gated, and reversible. Order is
chosen so nothing writes a live surface until the data underneath is trusted.

### Phase 0 — Extract and archive *(no user-visible change)*
- Promote `gantt_extract.py` to `scripts/extract_legacy_gantt.py`
- Run Migration C; persist all 174 bars to `gantt_legacy_bars`
- **Exit criterion:** row count matches the extractor's, and spot-checking 5
  bars against the sheet agrees on both dates

### Phase 1 — Start dates, proposed not applied
- Run Migration A
- `propose_project_starts()` emits one `project_start_proposal` per project
  carrying: the old-Gantt candidate (with its matched bar text), the
  earliest-task date, and which it recommends
- Eyal actions them via the existing `get_proposals` / `decide_proposal` path
- Approve → `start_date` + `manual_start_date = TRUE`
- **Exit criterion:** every active project has a start date, or is explicitly
  marked as having none

### Phase 2 — Timeline tab, read-only
- Render the tab from the DB; **no readback**
- Row grouping for the task waterfall; priority colours; today marker
- Registered in `NON_AREA_TABS` from day one
- **Exit criterion:** a week of the tab matching what Eyal believes is true,
  with no reconcile interference

### Phase 3 — Milestones
- Run Migration B
- Seed from the `Company OKRs` bars and the ★ rows, again as proposals
- CEO tab renders milestones + the manual management block
- **Exit criterion:** the slipped "Signing #1 MVP client" renders with both its
  original and current date

### Phase 4 — Bidirectional edit
- Dragging a bar is *not* supported; editing dates in cells is
- Reuse the Project Status reconcile machinery wholesale — snapshot as merge
  base, `manual_*` rails, `unresolved_columns` layout gate, cell-write batch
- Ships in **shadow mode first**, exactly as Project Status v2 did
- **Exit criterion:** a clean shadow week

### Phase 5 — Retire or repoint the old board
- Only after Phases 0–4 are settled
- Fix the `#REF!` errors regardless — that board is still what Eyal reads today

---

## 7. Risks and guards

| Risk | Guard |
|---|---|
| **`GANTT_SHADOW_MODE=False` writes the old board with no preview.** `GANTT_CUTOVER_PREVIEW` is documented as the abort gate and has **zero code references** — it gates nothing. | Do not flip it. This plan never writes the old board; Phase 5 is the only step that touches it, and the preview must be implemented first. |
| Auto-seeded start dates land silently wrong (`Corporate → 2027-05-31`) | Every seed is a proposal. Nothing writes a start date without Eyal approving it. |
| The bar redraws on its own as tasks change | Freeze the derived start on first computation; thereafter frozen-or-manual. |
| New tabs get parsed as area tabs and stripped | `NON_AREA_TABS`, imported not duplicated, plus a test asserting membership. |
| Formatting stacks up on every refresh | Assert the whole state, never add to it. Delete own conditional-format rules and protected ranges before re-adding — five of fifteen defects in the 2026-08-09 review were this. |
| A migration backfills a decision nobody made | No `DEFAULT` on `start_date`. Verify migrations by probing the **new column** (`select(col)` → 42703 = absent), never a value a prior migration could have set. |
| Milestone slippage is silently overwritten | `original_date` is written once and never updated. |

---

## 8. Testing

- **Extractor**: fixture board → known bars, including the year-rollover case
  (a December→January column pair must not produce a backwards date)
- **Three-tier start rule**: manual wins; frozen beats derived; derived freezes
  on first computation and does not move when an older task is attached
- **Milestone slippage**: moving `target_date` preserves `original_date`
- **Tab registration**: both tabs in `NON_AREA_TABS`; headers do **not** satisfy
  `unresolved_columns` (belt and braces, as with Focus)
- **RLS**: `tests/test_rls_coverage.py` covers the two new tables automatically
- **No positional mocks.** Fixtures key on the argument, not call order — three
  separate test breakages this month came from positional `side_effect` lists.

---

## 9. Decisions

### Settled 2026-08-12

1. **Bar end when no `target_date`** — open-ended to the right; Eyal closes it
   by hand. Not derived from task deadlines. (§5)
2. **Horizon and resolution** — weekly columns, one tab, 2026-03 → 2027-12
   (~96 columns). No yearly tabs. (§5)
3. **Bidirectional** — yes at project level (start, target, owner); task rows
   read-only on the Timeline. Conflicts reported, never guessed. (§5)

### Still open

4. **Retired projects** — show greyed with their historical span, or drop them?
5. **Milestone ownership** — the ★ rows carry `[E]`/`[E/P]` prefixes. Does a
   milestone get an owner, or is every milestone the CEO's?
6. **Does the old board keep running in parallel**, and for how long?
7. **Legacy overlay** — worth rendering the Phase 0 bars as a "what we planned
   in March" ghost layer on the same grid? It is nearly free given the spans
   now match, but it doubles what is on screen.

---

## 10. Explicitly not doing

- **Dependencies / critical path.** No dependency data exists and inventing it
  would be false precision on a 4-person team.
- **Dragging bars.** Editing dates in cells is enough and far cheaper to make
  safe.
- **Auto-generating OKRs or escalations.** No source; manual is honest.
- **Touching the 2028-2029 / 2030-2031 tabs.** Out of scope.
- **Deleting the old workbook.** It is the only record of 174 dated bars until
  Phase 0 archives them, and it stays as reference afterwards.
