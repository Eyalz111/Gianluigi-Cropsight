# Proposed fix — live cross-tier leak to the founding team (P2-01 / P2-02)

Status: **PROPOSAL — no code changed.** Gianluigi proposes, Eyal approves.
Source: system audit Phase 2 (`SYSTEM_AUDIT_2026_06.md`). Date: 2026-06-11.

These two are pulled out ahead of the rest of the audit because they are **live
distribution paths in production** that can put CEO-tier content in front of
Roye / Paolo / Yoram. Both are conditional (they require a CEO-tagged item to
exist in the week's / meeting's content), and both are gated only by Eyal's
manual eyeball, not by code. The fix is small and there is already a correct
reference implementation in the codebase to copy.

The invariant: **I3 — sensitivity follows data; any team-facing output must
filter out CEO-tier content.** Tiers: `public(1) < team(2) < founders(3) < ceo(4)`.
The team cap is `founders (3)` — drop only CEO-tier (4).

The helper already exists and is the right tool:
`models/schemas.py:99` — `filter_by_sensitivity(items: list[dict], max_level: int)`
(missing `sensitivity` defaults to founders; legacy `ceo_only/restricted/sensitive`
map to CEO/4). `_FOUNDERS = 3`.

---

## P2-01 — Weekly digest emails unfiltered CEO content to the team  (HIGH)

**Path.** `processors/weekly_digest.generate_weekly_digest()` builds the digest:

```
weekly_digest.py
 62  decisions      = await get_decisions_for_week(week_start, week_end)   # list_decisions — NO tier filter
 63  task_summary   = await get_task_summary()                             # get_tasks      — NO tier filter
 64  open_questions = await get_open_questions_summary()                   # get_open_questions — NO tier filter
 90  digest_document = format_digest_document(decisions, task_summary, open_questions, ...)
110  return { "digest_document": digest_document, ... }
```

…and `guardrails/approval_flow.distribute_approved_digest()` emails that document
to the whole team in production:

```
approval_flow.py
2385  digest_emails = settings.team_emails        # production → Roye/Paolo/Yoram
2387  await gmail_service.send_weekly_digest(recipients=digest_emails, digest_content=digest_doc, ...)
```

**The proof it's a bug, not a deliberate choice.** The on-demand team email
(`processors/weekly_team_package.py:109-112`) calls the **identical builders**
and wraps each one:

```python
decisions = filter_by_sensitivity(await get_decisions_for_week(week_start, week_end), _FOUNDERS)
done      = filter_by_sensitivity(task_summary.get("completed_this_week", []), _FOUNDERS)
overdue   = filter_by_sensitivity(task_summary.get("overdue", []), _FOUNDERS)
```

The scheduled digest path simply never received the same treatment.

**Failure scenario.** A meeting this week produced a CEO-tagged decision
("accept BigAg's $5M term sheet") or a CEO-tagged task. It sits in the DB with
`sensitivity='ceo'`. `get_decisions_for_week` / `get_task_summary` return it
(they filter to *approved*, not by *tier*). It is rendered into `digest_document`
and emailed to Roye/Paolo/Yoram. Eyal's only defense is spotting one line in a
long digest at approval time.

**Proposed change (filter at the builder feed, inside `generate_weekly_digest`):**

```python
# weekly_digest.py — top of file
from models.schemas import filter_by_sensitivity
_FOUNDERS = 3   # team cap: drop CEO-tier only (mirrors weekly_team_package)

# in generate_weekly_digest(), replace lines 62-64:
decisions = filter_by_sensitivity(
    await get_decisions_for_week(week_start, week_end), _FOUNDERS)
task_summary = await get_task_summary()
for _k in ("completed_this_week", "overdue", "due_next_week"):
    task_summary[_k] = filter_by_sensitivity(task_summary.get(_k, []), _FOUNDERS)
open_questions = filter_by_sensitivity(
    await get_open_questions_summary(), _FOUNDERS)
```

(If `OUTPUTS_PRIORITY_URGENCY_AREA_ENABLED` is on, the `by_area`/`by_urgency`
rollups in `get_task_summary` are computed *inside* that function over the raw
lists — recompute them from the filtered lists, or compute the rollups after the
filter. Easiest: do the filtering inside `get_task_summary` before the rollup
block at `weekly_digest.py:246`, so the counts also exclude CEO items.)

**Blast radius.** `generate_weekly_digest` feeds only the team digest + its
approval preview. Filtering here means Eyal's preview also shows the
founders-capped view — correct, since he is approving the *team's* copy. CEO
items still reach Eyal through the morning brief / weekly review / pulse
(Eyal-only paths). No other caller.

**Effort:** S–M.

---

## P2-02 — Meeting-summary email leaks via the prose, not the structured list  (HIGH)

**Path.** `guardrails/approval_flow.distribute_approved_content()` correctly
strips CEO items from the *structured* lists for the team copy…

```
approval_flow.py
2016  if sensitivity not in ceo_tiers:
2017      filtered_decisions = [d for d in content["decisions"]      if d.get("sensitivity") not in ceo_tiers]
2018      filtered_tasks     = [t for t in content["tasks"]          if t.get("sensitivity") not in ceo_tiers]
2019      filtered_questions = [q for q in content["open_questions"] if q.get("sensitivity") not in ceo_tiers]
2026      team_content = {**content, "decisions": ..., "tasks": ..., "open_questions": ...}
```

…but then emails the **raw rendered prose** and the **unfiltered**
`discussion_summary` to the founders/team distribution list:

```
2069  summary_content=summary,                              # content["summary"] — pre-rendered prose, UNFILTERED
2075  discussion_summary=team_content.get("discussion_summary", ""),   # team_content never replaced this key → original prose
```

`summary` (set at `2069` from `content.get("summary")`, line 1841) and
`discussion_summary` are free-text generated at extraction from the whole
transcript — if a CEO-tagged item is restated in that prose, the structured
filter doesn't touch it and it ships to the team.

**Failure scenario.** A FOUNDERS-tier meeting contains one CEO-tagged decision.
The structured task/decision list the team sees is clean, but the narrative
summary paragraph mentions the decision in prose → Roye/Paolo receive it by email.

**This one needs a decision — two options:**

- **Option A (fast, low-risk interim):** when the audience is non-CEO **and**
  `has_filtered` is true (CEO items were stripped), do not send the prose body
  to the team — send the filtered structured content + the Drive link only, with
  a one-line "full narrative in the linked doc (Eyal-only)" note. Pure control
  flow, no new LLM call. Downside: team loses the narrative for mixed-tier
  meetings.

- **Option B (durable, recommended):** generate a **team-safe summary string**
  filtered to founders at approval time and email that. Concretely: render the
  summary prose from `team_content` (the already-CEO-stripped dict) via the same
  formatter that produced `content["summary"]`, store it as
  `content["team_summary"]`, and change line 2069 to
  `summary_content=content.get("team_summary") or summary` and line 2075 to the
  team-filtered discussion summary. Costs one extra render (LLM or template,
  depending on how `summary` is produced) only on the mixed-tier path.

Recommendation: **B**, because the narrative is the most-read part of the email
and silently dropping it (A) degrades the team's main artifact. A is acceptable
as a same-day stopgap if B can't land immediately.

**Note:** the weekly-review Drive digest (P2-03, `approval_flow.py:2817-2837`)
is the same root cause — its `digest_content` is built from the same unfiltered
`decisions`/tasks and shared via a team-emailed Drive link, under a misleading
`# Email to team (sensitivity-aware)` comment. Fold it into the same fix:
`filter_by_sensitivity(decisions, _FOUNDERS)` before the `for d in decisions`
loop at 2820, and the same for the completed/overdue task loops.

---

## Why these are grouped (and why the rest of the I3 findings ride along later)

The whole class is one missing step: **the per-item CEO filter exists in two
places (`distribute_approved_content` structured lists + `weekly_team_package`)
but was never applied at six other team-facing render points** — digest builders
(P2-01), summary prose (P2-02), review Drive digest (P2-03), `follow_up_meetings`
(P1-05), ingested documents (P1-09), knowledge `_rag_chunks` (P2-09). A single
`filter_by_sensitivity(..., _FOUNDERS)` at each render closes all of them.
P2-01/02/03 are carved out here because they are the *live email paths*; the
rest are lower-traffic or internal and can go in the Phase-7 fix sequence.

## Suggested test plan (before approving any of this)
1. Unit: seed a decision/task/open_question with `sensitivity='ceo'`; assert it
   is absent from `generate_weekly_digest()['digest_document']` and from the
   `weekly_team_package` output; assert a `founders` item is present.
2. Unit: a FOUNDERS meeting with one `sensitivity='ceo'` decision whose text also
   appears in `content['summary']` → assert the team email body (Option B
   `team_summary`) does not contain the CEO text; Eyal's copy does.
3. Regression: confirm CEO-*whole-meeting* path still routes Eyal-only
   (`get_distribution_list('ceo', …)` already returns Eyal-only — unchanged).
4. Hermetic: no live Google/Telegram (conftest guard); no live-DB writes.
