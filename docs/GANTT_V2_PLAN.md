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
expressible: the row keeps its first commitment and shows the move, instead of
quietly changing.

**No reason field. Decided 2026-08-12.** A first draft of this plan proposed
recording *why* a milestone moved — delay vs deliberate postponement vs added
scope. Eyal rejected it: *"I don't see a reason to have a 'reason per move'
that will force me to add explanations or even worse, will try to guess it
automatically."*

He is right on both halves. A required reason is friction on every move, and an
optional one is an empty slot that something will eventually be tempted to fill
by inference — which would be the system asserting intent it cannot observe.

So the model records **the fact and nothing else**: the dates, and when they
changed. `original_date` never moves; `target_date` does; a `milestone_moves`
child row records each `(from, to, moved_at)`. Any explanation lives in the
free-text label, exactly as the old board already does it — written by a human
who felt like writing it, never prompted for and never inferred.

The CEO tab therefore shows *"moved 1 Jun → 6 Jul"*, not *"SLIPPED"*.
Whether that was a slip or a re-plan is Eyal's read, and the board should not
put a word in his mouth.

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

**Status, 2026-08-12.** Phases 0, 2 and 3 are built and merged — Phase 2
including the legacy archive block (decision 7). Phase 1's 22 proposals are
emitted and waiting on Eyal, and they gate everything visible: until a start
date is approved the live half of the Timeline draws **zero bars**. Phase 3
needs its migration run before it can propose. Phases 1b, 4 and 5 are not
started.

### Phase 0 — Extract and archive *(no user-visible change)* — **DONE**
- Promote `gantt_extract.py` to `scripts/extract_legacy_gantt.py`
- Run Migration C; persist all 174 bars to `gantt_legacy_bars`
- **Exit criterion:** row count matches the extractor's, and spot-checking 5
  bars against the sheet agrees on both dates

### Phase 1 — Start dates, proposed not applied — **WAITING ON EYAL**
- Run Migration A
- `propose_project_starts()` emits one `project_start_proposal` per project
  carrying: the old-Gantt candidate (with its matched bar text), the
  earliest-task date, and which it recommends
- Eyal actions them via the existing `get_proposals` / `decide_proposal` path
- Approve → `start_date` + `manual_start_date = TRUE`
- **Exit criterion:** every active project has a start date, or is explicitly
  marked as having none

### Phase 1b — the five projects with no signal at all — **NOT STARTED**

Decision 4 (keep retired projects, greyed) does not render on its own. A grey
row is still a row that needs a left edge: `span_columns()` returns `(-1, -1)`
when `start_date` is `None`, so those projects appear as a name against an empty
96-week grid — present but blank, which reads as a bug rather than as history.

The pass ran on 27 projects and emitted **22** proposals. Measured 2026-08-12,
the five it skipped are:

```
Moldova Pilot                 retired   no tasks
Operational Tooling           retired   no tasks
Others - Cleints improvment   retired   no tasks
Team & HR                     retired   no tasks
Others — Fundraising          active    no tasks
```

`propose_project_starts` **already includes retired projects** and already
consults the archive — an earlier draft of this section wrongly said it needed
extending. These five fall out for a different reason: no tasks to derive from,
*and* no archived bar whose name match survives the area gate. There is no
signal, and inventing one is the failure mode the whole phase exists to avoid.

So this is a hand-typed date, not an algorithm — but not an unaided one. The
archive block shipped with the ghost layer puts the old board's own lanes on the
same grid, which is the evidence Eyal would need to type them from.

- **Exit criterion:** each of the five either draws a grey bar over a real
  historical span, or is explicitly marked as having no recoverable start

### Phase 2 — Timeline tab, read-only — **BUILT, FLAG OFF**
- Render the tab from the DB; **no readback**
- Row grouping for the task waterfall; priority colours; today marker
- Registered in `NON_AREA_TABS` from day one
- **Exit criterion:** a week of the tab matching what Eyal believes is true,
  with no reconcile interference

Shipped as `processors/timeline_view.py` (shape, no I/O) + `services/timeline_sheet.py`
(Sheets only), hooked into the reconcile cycle behind `TIMELINE_VIEW_ENABLED`,
default off. The whole tab is protected `warningOnly` because an edit there would
vanish on the next refresh until Phase 4 lands.

Two things learned building it, both worth carrying into Phase 3:

*The week grid must be wiped white before any bar is painted.* `values().clear()`
removes text and never formatting, so a bar that moved or shortened leaves its
old cells coloured — the ghost of a previous plan sitting on the board looking
like current truth. Pinned by a test that asserts the wipe precedes the fills.

The **legacy archive block** (decision 7) ships in the same tab: `legacy_archive`
in the processor, rendered below the live rows and collapsed, behind
`TIMELINE_LEGACY_OVERLAY_ENABLED`. Meeting lanes and the `OPERATIONAL RULES`
section are dropped as not-plan — 99 of the 244 surviving bars — leaving 28 lane
rows and 144 bars. Two mechanical traps are pinned by tests: a bar with a blank
end is **one week**, never open-ended (these are runs of filled cells, so a blank
end means the run was one cell — treating it as open would paint 90-odd weeks of
lilac), and `addDimensionGroup` *creates* a group without closing it, so the
block arrives collapsed only because a second `updateDimensionGroup` says so.

*A bar needs a left edge, which means Phase 1 gates Phase 2 completely.* The tab
renders correctly with zero bars today because no start date is approved yet.
Five projects — the 4 retired ones plus Others—Fundraising — have no derivable
start at all and need a hand-typed date; the area gate correctly refuses to
borrow, for instance, Moldova Pilot's start from a bar sitting in the Sales
section.

### Review, 2026-08-12 (max) — six fixes

Fifteen findings, which were five distinct defects duplicated two and three
times: the synthesis and sweep agents died on a spend limit, so nothing merged
them. Coverage is therefore good but not exhaustive.

**The most-confident finding was wrong.** Three independent finders CONFIRMED
that the tab must fail with a grid-limits 400, because `_ensure_tab` issues a
bare `addSheet` (26 columns) while every range targets 101. The mechanism is
real; the conclusion is not. Production had already rendered 160 rows across 101
columns — `values().update` expands the grid on its way past the edge. Checked
against the live sheet before reporting. Worth remembering that this review's
confidence and its correctness came apart on exactly the finding it repeated
most.

Fixed:

1. **`propose_project_starts` had no caller.** The proposer was correct and
   fully tested; nothing ran it. The 22 queued proposals came from a one-off
   manual run, and every project created afterwards would have been skipped in
   silence — no start date, so no bar, forever. Now rides the daily QA beside
   `_run_project_learning`, and is idempotent.
2. **The today-marker accumulated.** `updateBorders` is not touched by a
   `backgroundColor` wipe, so last Monday's red line stayed and a new one
   appeared beside it every week. Now cleared with an explicit `style: "NONE"`
   pass first, the same shape `project_status_sheet.py:441` already uses.
3. **`values().clear()` was sized from the NEW grid** (`len(grid) + 40`), so a
   render that shrank by more than 40 rows left the previous tail as text — and
   since the fill wipe reaches 200 rows, it showed as rows with no bar under
   them. Now an unbounded column range, with no arithmetic to get wrong.
4. **The wipe skipped columns A–E.** Area headers and the archive title paint
   across all columns, so their fills survived in the label block at whatever
   row they last occupied. Now starts at column 0 and resets `textFormat` too,
   because those rows are bold.
5. **A gantt-only proposal could not be approved.** When a project had an
   archived bar but no tasks, `recommended` was `None` and `apply_project_start`
   refused it — an un-clearable card. The archive is now the recommendation of
   last resort, labelled `recommended_source: gantt_bar`, with the matched
   label still on the card.
6. **Grid width is now asserted, not inherited.** Not a defect (see above), but
   the tab is 101 columns only by an undocumented side effect. One
   `updateSheetProperties` before the first ranged write makes it a stated
   invariant.

### Phase 3 — Milestones — **BUILT, FLAG OFF, MIGRATION NOT RUN**

`scripts/migrate_milestones.sql` + `processors/milestones.py` (collapse, propose,
apply, move) + `services/ceo_sheet.py` (the tab), behind `CEO_TAB_ENABLED`
(default off) and `MILESTONE_SEEDING_ENABLED` (default on, but silent until the
migration exists). Seeding rides the daily QA — wired from the start, because
Phase 1 shipped a proposer with no caller.

**Exit criterion met on real data.** 392 archived bars collapse to **16
milestones**, and `Signing #1 MVP client` renders with both its dates: it is
written at 2026-06-01 and again as `- postponed here` at 2026-07-06, in *two*
lanes — four bars for one commitment and one move. Grouping on the row rather
than the DATE would have recorded three moves for one postponement.

Where they actually live — not the empty `STRATEGIC MILESTONE` section, and the
board's own glyphs turn out to carry the kind:

```
★  Technology     product      MVP Product Delivery, Product V1 Target
●  Commercial     commercial   First MVP Delivery, 1st Paying Client, Q-goals
◆  Funding        funding      Pre-Seed Round timing
   Company OKRs   by title     Q1 OKR, Investor's Package, Signing #1
```

`Strategy & Decisions` is deliberately **not** a source: a monthly investor
update repeating to 2027-11, an annual offsite, two decisions. That is
recurrence and decision history, and the Meetings pool already models
recurrence.

Two things worth carrying forward:

*The management block is hand-maintained on a tab that regenerates every 30
minutes.* The refresh reads the block back and re-emits it below the milestones,
so it moves as the list grows instead of being clipped by a fixed boundary — and
if that read fails the write is **abandoned**, because a stale tab is
recoverable and a person's own words are not. Only the generated half is
protected; protecting the whole tab would make the block read-only and the
design pointless.

*The history cell says "moved 1 Jun → 6 Jul", never "SLIPPED".* A test asserts
no verdict word appears in it at all. Whether a move was a slip or a re-plan is
Eyal's read.

**Remaining before it can render:** run the migration, approve the 16 proposals,
flip `CEO_TAB_ENABLED`.

### Phase 4 — Bidirectional edit
- Dragging a bar is *not* supported; editing dates in cells is
- Reuse the Project Status reconcile machinery wholesale — snapshot as merge
  base, `manual_*` rails, `unresolved_columns` layout gate, cell-write batch
- Ships in **shadow mode first**, exactly as Project Status v2 did
- **Exit criterion:** a clean shadow week

### Phase 5 — Freeze the old board *(decided 2026-08-12)*

Eyal: *"dont make it run in parallel, just dont delete it."* Freeze, keep,
never write. Only after Phases 0–4 are settled.

The board is **already never painted** — `GANTT_SHADOW_MODE` defaults `True`
and is not overridden in prod, and `reconcile_scheduler._run_gantt` is
documented "Never paints the board". So there is no write to stop. What is
still live is the weekly **read**: `reconcile_gantt_lanes()` pulls board →
knowledge, and `compute_gantt_nudges()` derives nudges from it.

That read is what "running in parallel" actually means here, and it is the
thing that must stop. A frozen board keeps feeding March lane text into
knowledge forever, and the nudges would compare the brief against a board
nobody maintains.

- **Cutover action: `GANTT_RECONCILE_ENABLED=false`.** One flag; that is the
  whole parallel-running surface.
- **Leave the workbook in Drive, untouched.** It is the record of 174 dated
  bars, and `gantt_legacy_bars` is a copy, not a replacement — the copy holds
  no formatting, no ★ styling, no section structure.
- **Convert the live formulas to static values at freeze time**, and fix the
  `#REF!` cells in the same pass. The original plan said fix them "regardless —
  that board is still what Eyal reads today"; under decision 6 it stops being
  what he reads, which makes the case *stronger*, not weaker. An archive with
  live formulas keeps re-evaluating against tabs nobody is maintaining, so it
  can rot further after we stop looking at it. Values freeze; formulas decay.
- **No deletion, ever.** Already in §10.

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
4. **Retired projects** — stay on the board, greyed, with their historical span.
   Already built this way in Phase 2 (`_status_of` → `completed` → `#D0D0CC`).
   Consequence: a grey row still needs a start date to draw a bar, and the four
   retired projects have none — see Phase 1b. (§6)
5. **Milestones have no owner.** Eyal: *"milestone has no owner because it is a
   company thing."* So Migration B carries no `owner` column, and the `[E]` /
   `[E/P]` prefixes on the old ★ rows stay as legacy text inside the label —
   they are never promoted to a field. A milestone belongs to the company. (§4)
6. **The old board does not run in parallel.** Eyal: *"dont make it run in
   parallel, just dont delete it."* At cutover it becomes a frozen archive, not
   a second live surface. What that means concretely is in Phase 5. (§6)

7. **Legacy overlay — yes, and it makes no per-project claim.** Eyal approved
   the ghost layer. Built as a **collapsed archive block** below the live rows:
   the old board's own sections and lanes, redrawn on the new columns.

   The obvious shape — each project's matched legacy bar drawn directly beneath
   it — was built first and dropped, because it cannot be made honest. Measured
   on live data it drew **2 ghosts across 27 projects**, and the matches it
   suppressed included obviously-correct ones scoring *identically* to obvious
   junk:

   ```
   Legal      -> "Legal entity Establishment"            1 shared word   RIGHT
   Italy      -> "Aquiring MVP Clients: Italy"           1 shared word   RIGHT
   Corporate  -> "Monthly close — send docs to Shimony"  1 shared word   WRONG
   Others—CD  -> "Signing #1 MVP client"                 1 shared word   WRONG
   ```

   No threshold admits the first two without the second two. Raising the bar to
   two shared words silences the feature; lowering it ships confident nonsense
   under a project's own name, which is worse than shipping nothing.

   The archive asserts nothing instead: **28 lane rows, 144 bars**, the old
   board's structure on the new grid, and Eyal reads the correspondence himself.
   Same instinct as the rest of this plan — show the evidence, never infer the
   link. Collapsed by default, so "it doubles what is on screen" is answered:
   it does not, until asked. `TIMELINE_LEGACY_OVERLAY_ENABLED`, default on.

### Still open

*(nothing — 1 through 7 are settled)*

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
