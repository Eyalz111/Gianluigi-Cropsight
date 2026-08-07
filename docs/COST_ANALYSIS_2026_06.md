# Gianluigi — End-to-End Budget & Cost Analysis (June 2026)

*Grounded in live spend (`get_cost_summary`, the `token_usage` table) + a full code-level map of every cost surface. Unit prices for infra/external APIs are estimates — confirm against the actual GCP/Supabase bills (flagged in §7).*

---

## 1. Bottom line

| Bucket | Est. monthly | Notes |
|---|---|---|
| **Claude LLM** | **~$15–25** | Measured: $14.22 over the last ~3 weeks (see §2). Dominated by transcript extraction. |
| **Cloud Run (always-on instance)** | **~$25–70** | The single largest line. Fixed — runs 24/7 (`min-instances=1`, `--no-cpu-throttling`, 1 vCPU / 1 GiB). **Confirm vs. GCP bill.** |
| **Supabase (database)** | **~$0–25** | Free tier historically; Pro is $25 if exceeded. |
| **Perplexity / ElevenLabs (intelligence signal, voice)** | **$0** | All those features are flag-gated **OFF** today. |
| **Google APIs (Drive/Calendar/Sheets/Gmail)** | **$0** | Free within quota. |
| **TOTAL run-rate** | **≈ $40–120 / month** | Infra (Cloud Run) + transcript extraction are the two things that move this number. |

**The headline:** your recurring cost is **mostly fixed infrastructure**, not AI. The always-on Cloud Run instance likely costs more per month than all the Claude calls combined. The biggest *AI* lever is meeting transcript extraction (~$0.51 per meeting, at Opus).

---

## 2. Live LLM actuals — last ~3 weeks (2026-05-24 → 06-14)

Measured from the `token_usage` table (every Claude call is logged):

**Total: $14.22** (≈ **$0.65/day**, ≈ **$20/month** run-rate).

### By model
| Model | Cost | Share | Calls | Why |
|---|---|---|---|---|
| **Opus** (`claude-opus-4-6`) | **$11.49** | **81%** | 32 | Accuracy-critical: transcript extraction + area-brief synthesis |
| **Sonnet** (`claude-sonnet-4-6`) | $1.62 | 11% | 110 | Agent conversations, edits, prep, email extract |
| **Haiku** (`claude-haiku-4-5`) | $1.11 | 8% | 858 | Cheap classification/routing (most calls, least cost) |

### By feature (top drivers)
| Feature | Cost | Calls | Unit |
|---|---|---|---|
| **transcript_extraction** | **$9.69** | 19 | **~$0.51 / meeting** |
| area_brief_synthesis | $1.28 | 12 | knowledge layer (nightly/weekly) |
| topic_brief_synthesis | $0.82 | 63 | knowledge layer |
| intelligence_signal_synthesis | $0.51 | 1 | one weekly signal |
| edit_application | $0.39 | 7 | re-applying your edits to a summary |
| email_extract | $0.21 | 26 | per relevant email |
| task_dedup / status_inference / question_resolution / completeness / supersession | ~$0.70 combined | ~120 | the per-meeting "Haiku tax" |
| everything else (routing, prep, headlines, title-match, sensitivity…) | ~$0.15 | ~700 | negligible individually |

**Read:** ~70% of all LLM spend is the 19 meetings you processed. Each full meeting costs ~**$0.51** at Opus plus ~$0.03 of Haiku helpers ≈ **~$0.55/meeting end-to-end**. Everything else (conversations, briefs, emails, routing) is small change.

> Note: the daily trend shows spikes on meeting-processing + knowledge-synthesis days ($2.21 on 05-31, $2.11 on 06-02, $2.06 on 06-13) and near-zero on quiet days ($0.001 on 06-05) — i.e. spend tracks meeting volume + whether the knowledge schedulers ran.

---

## 3. The model tiers (how cost is controlled)

All Claude calls go through one gateway (`core/llm.py`), tiered to balance accuracy vs. price:

| Tier | Model | Input / Output ($/Mtok) | Used for |
|---|---|---|---|
| **Opus** | claude-opus-4-6 | **$15 / $75** | Transcript extraction, document analysis, knowledge synthesis — *accuracy-critical* |
| **Sonnet** | claude-sonnet-4-6 | $3 / $15 | Conversations, tool use, edits, meeting prep |
| **Haiku** | claude-haiku-4-5 | $0.80 / $4 | Classification, routing, dedup — *high volume, cheap* |

Long system prompts are **prompt-cached** (cache reads are ~10× cheaper than fresh input), which is why 858 Haiku calls cost only $1.11. There's a built-in **$5/day cost alert** threshold (`DAILY_COST_ALERT_THRESHOLD`) — your actual average is well under that.

---

## 4. Infrastructure — the real fixed cost

**Cloud Run** (`europe-west1`, project `gianluigi-488420`): `1 GiB / 1 vCPU`, `min-instances=1`, `max-instances=1`, `--no-cpu-throttling`.

- `min-instances=1` + `--no-cpu-throttling` = **one instance fully allocated 24/7** (it has to stay alive for Telegram long-polling + the schedulers). This bills continuously regardless of activity.
- Estimated **~$25–70/month** at full always-on allocation (europe-west1 vCPU-second + GiB-second rates). **This is almost certainly your single biggest monthly line — bigger than all LLM spend.** Pull the actual GCP billing line to confirm.

**Supabase**: Postgres + pgvector, provisioned free-tier historically. Grows with data; the `token_usage` table adds one row per LLM call. Likely **$0–25/month**.

---

## 5. What's OFF (latent cost, $0 today)

These are deliberately flag-gated off — flipping any on **raises spend**:

| Feature | Flag | If turned on |
|---|---|---|
| **Transcript watcher** | `TRANSCRIPT_WATCHER_ENABLED` | *Already ON in prod* — this is what processes your meetings (~$0.55 each). |
| Intelligence signal | `INTELLIGENCE_SIGNAL_ENABLED` (off) | Opus synthesis + ~10 Perplexity queries (+optional ElevenLabs video) ≈ $0.20–0.80/week |
| Knowledge schedulers | `KNOWLEDGE_NIGHTLY/WEEKLY_ENABLED` | Loops Opus/Sonnet per topic/area nightly — the area/topic-brief lines you see in §2 |
| Voice in/out | `VOICE_INTAKE / VOICE_OUT_ENABLED` (off) | ElevenLabs ~$0.30/1K chars |

> ⚠️ `KNOWLEDGE_SHADOW_MODE`, while on, **doubles** the knowledge-synthesis cost (runs old + new in parallel). Worth turning off once you've cut over.

---

## 6. Levers to reduce cost (in order of impact)

1. **Cloud Run is the big one.** If 24/7 latency isn't required, `min-instances=0` + CPU throttling would cut the dominant fixed cost dramatically — **but** it breaks Telegram long-polling and the schedulers (the bot must stay alive). Realistic options: keep as-is, or migrate Telegram to webhooks (allows scale-to-zero) — a real project, not a flag. *Biggest savings, biggest effort.*
2. **Transcript extraction is ~70% of LLM spend.** It's already on Opus for accuracy. If some meetings don't need that fidelity, a Sonnet tier for routine meetings would cut per-meeting cost ~5× ($0.51 → ~$0.10). Trade-off: extraction quality.
3. **Turn off `KNOWLEDGE_SHADOW_MODE`** once cut over — removes the 2× knowledge-synthesis overhead.
4. **Dedupe meeting processing** — see §8: the 06-13 meeting was processed twice (two transcript files). Each duplicate = a wasted ~$0.51 extraction. Tightening which files reach the watcher avoids that.
5. Everything else (conversations, briefs, emails, routing) is already cheap — not worth optimizing.

---

## 7. What you should confirm (unknowns)

1. **The actual GCP Cloud Run bill** — my $25–70 is an estimate; the real number settles the total. (Billing → Cloud Run line.)
2. **Supabase tier** — still free, or upgraded to Pro ($25)?
3. **Anthropic published rates** — I used Opus $15/$75, Sonnet $3/$15, Haiku $0.80/$4 (these match the hardcoded rates in `core/cost_calculator.py`). Verify against the current pricing page.
4. **Your real meeting volume/month** — at ~$0.55/meeting, 30 meetings/mo ≈ $16, 60 ≈ $33.

You can always pull live spend yourself: ask Claude.ai (CropSight Ops) for **"cost summary last 30 days"** — it calls the `get_cost_summary` tool and shows total + per-model + per-feature + daily trend, straight from `token_usage`.

---

## 8. Side note surfaced during this analysis

The **2026-06-13 "CropSight Monthly session"** was processed **twice** — once from a date-prefixed file (`2026-06-13CropSight…MVP PEP`, 100 min, which you **rejected**) and once from a non-prefixed file (`CropSight…MVP`, 206 min, which you **approved** this morning). Two transcript files for one meeting → two Opus extractions (~$1 instead of $0.51). Worth checking whether Tactiq is dropping two files (partial + full) into the watched Drive folder — the watcher will extract each.

---

*Generated 2026-06-14. Sources: `core/llm.py`, `core/cost_calculator.py`, `config/settings.py`, the live `token_usage` table via `get_cost_summary`, and the Cloud Run deploy config in `CLAUDE.md`.*
