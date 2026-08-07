### processors/

processors/completeness_check.py | 154 | Completeness check (v2.5 PR4).
processors/cross_reference.py | 733 | Cross-reference processor for v0.3 Operational Intelligence.
processors/deal_intelligence.py | 206 | Deal intelligence processor (Phase 4).
processors/debrief.py | 1047 | End-of-day debrief and quick injection processor.
processors/decision_review.py | 53 | Decision review processor — surfaces decisions due for periodic review.
processors/document_processor.py | 600 | Document ingestion and processing.
processors/email_classifier.py | 320 | Email classification and intelligence extraction.
processors/entity_extraction.py | 563 | Entity extraction and linking for v0.3 Tier 2.
processors/gantt_intelligence.py | 243 | Gantt intelligence — computed metrics from existing Gantt data.
processors/gantt_linkage.py | 156 | PR-C: per-lane → topics linkage (DB-only).
processors/gantt_nudge.py | 124 | PR-E: weekly Gantt nudges (brief ↔ board divergence).
processors/gantt_readback.py | 102 | PR-D: weekly Gantt read-back (board → knowledge).
processors/gantt_restructure.py | 208 | PR-B: copy + add-rows engine (the FRONT change).
processors/gantt_slide.py | 341 | Gantt slide (PPTX) generator.
processors/gantt_tagging.py | 123 | Gantt onboarding tagging (v3 chunk 2).
processors/intelligence_signal_agent.py | 1333 | Intelligence Signal agent — main orchestration pipeline.
processors/intelligence_signal_context.py | 467 | Intelligence Signal context builder.
processors/intelligence_signal_prompts.py | 520 | Intelligence Signal prompts and formatters.
processors/knowledge_consolidation.py | 204 | Nightly knowledge consolidation (v2.5 PR7/8).
processors/knowledge_readback.py | 143 | Read-back context for extraction (v2.5 PR3).
processors/knowledge_synthesis.py | 536 | Knowledge synthesis (v2.5 PR2) — cold-start + reusable brief generation.
processors/meeting_continuity.py | 593 | Meeting-to-meeting continuity — cross-meeting context for extraction.
processors/meeting_prep.py | 1369 | Meeting preparation document generator.
processors/meeting_type_matcher.py | 245 | Meeting type matcher — classifies calendar events.
processors/morning_brief.py | 1386 | Morning brief processor.
processors/operational_snapshot.py | 232 | Operational snapshot — compressed daily state summary.
processors/prep_ping.py | 286 | Meeting-prep "Prep Ping" (v2.5 Phase 3, chunk 3).
processors/proactive_alerts.py | 544 | Proactive alerts processor for v0.3 Tier 2.
processors/rollout_plan.py | 88 | Rollout plan — the staged env-flag cutovers.
processors/sheets_sync.py | 996 | Sheets on-demand sync processor (Phase 11 C7).
processors/summary_context.py | 145 | Executive-context clause builders for the meeting summary.
processors/summary_rich.py | 315 | Forward-facing rich meeting summary (PR7).
processors/task_signal_detection.py | 304 | Task signal detection — Phase 12 A5.
processors/topic_clustering.py | 180 | Topic clustering -> consolidation proposals (v2.5 PR10).
processors/topic_threading.py | 695 | Topic threading — track how projects/topics evolve.
processors/transcript_processor.py | 1253 | Transcript processing pipeline.
processors/weekly_digest.py | 757 | Weekly digest generator.
processors/weekly_pulse.py | 303 | Weekly Pulse — the deterministic Friday report.
processors/weekly_report.py | 218 | HTML weekly report generator.
processors/weekly_review.py | 366 | Weekly review data compilation.
processors/weekly_review_session.py | 875 | Interactive weekly review session processor.
processors/weekly_team_package.py | 198 | Weekly team package — the on-demand, tier-filtered team email.

### services/

services/alerting.py | 100 | Tiered system alerting for Gianluigi.
services/cloud_run_admin.py | 82 | Cloud Run admin client — one method, used by the rollout orchestrator.
services/conversation_memory.py | 164 | In-memory conversation history for Telegram and email interactions.
services/dropbox_sync.py | 214 | Dropbox → Google Drive sync service — Phase 13 B1.
services/elevenlabs_client.py | 191 | ElevenLabs client — text-to-speech + speech-to-text.
services/embeddings.py | 669 | Text embedding service for semantic search.
services/gantt_manager.py | 1294 | Gantt Manager — Core service for reading/writing the operational Gantt.
services/gantt_rows.py | 129 | Gantt row-tag plumbing (v3 chunk 2).
services/gantt_weeks.py | 90 | Week calculation utilities for Gantt chart column mapping.
services/gmail.py | 931 | Gmail API integration for Gianluigi email operations.
services/google_calendar.py | 304 | Google Calendar API integration.
services/google_drive.py | 1073 | Google Drive API integration.
services/google_sheets.py | 2170 | Google Sheets API integration.
services/health_server.py | 154 | Lightweight HTTP health check server for Cloud Run.
services/mcp_server.py | 3025 | MCP server for Gianluigi — Claude.ai as CEO dashboard.
services/perplexity_client.py | 193 | Perplexity API client for intelligence research queries.
services/supabase_client.py | 4993 | Supabase client for database operations.
services/telegram_bot.py | 4899 | Telegram bot for user interaction.
services/video_assembler.py | 1317 | Video assembler for Intelligence Signal news flash videos.
services/word_generator.py | 588 | Word document generator for meeting summaries and signals.

### schedulers/

schedulers/alert_scheduler.py | 130 | Proactive alert scheduler for v0.3 Tier 2.
schedulers/debrief_prompt_scheduler.py | 116 | Evening debrief prompt scheduler.
schedulers/document_watcher.py | 276 | Document watcher for detecting new team uploads.
schedulers/dropbox_sync_scheduler.py | 63 | Dropbox sync scheduler — Phase 13 B1.
schedulers/email_watcher.py | 507 | Email inbox watcher.
schedulers/intelligence_signal_scheduler.py | 166 | Intelligence Signal scheduler.
schedulers/knowledge_nightly_scheduler.py | 100 | Nightly knowledge-consolidation scheduler (v2.5 PR7/8).
schedulers/knowledge_weekly_scheduler.py | 117 | Weekly knowledge-synthesis scheduler (v2.5 PR9/10).
schedulers/meeting_prep_scheduler.py | 661 | Meeting preparation scheduler — Phase 5 redesign.
schedulers/morning_brief_scheduler.py | 122 | Morning brief scheduler.
schedulers/orphan_cleanup_scheduler.py | 316 | Orphan cleanup scheduler for v0.5.
schedulers/personal_email_scanner.py | 417 | Personal email scanner for daily scan of Eyal's personal Gmail.
schedulers/prep_ping_scheduler.py | 172 | Prep-ping scheduler (v2.5 Phase 3, chunk 3).
schedulers/qa_scheduler.py | 862 | QA Agent scheduler — Cross-cutting infrastructure X1.
schedulers/reconcile_scheduler.py | 139 | Reconcile scheduler (v3 outputs re-architecture).
schedulers/rollout_scheduler.py | 198 | Rollout orchestrator (v2.5 Phase 3, chunk 5).
schedulers/task_reminder_scheduler.py | 716 | Task reminder scheduler.
schedulers/task_sync_scheduler.py | 106 | Task sync scheduler — daily archival of completed tasks.
schedulers/transcript_watcher.py | 662 | Transcript watcher for detecting new Tactiq exports.
schedulers/weekly_digest_scheduler.py | 204 | Weekly digest scheduler.
schedulers/weekly_pulse_scheduler.py | 160 | Weekly Pulse scheduler (v2.5 Phase 3, chunk 4).
schedulers/weekly_review_scheduler.py | 366 | Calendar-driven weekly review scheduler.

### guardrails/

guardrails/approval_flow.py | 2943 | Eyal approval flow management.
guardrails/calendar_filter.py | 497 | Calendar filtering for CropSight vs personal meetings.
guardrails/content_filter.py | 433 | Content filtering for personal and inappropriate content.
guardrails/gantt_guard.py | 370 | Gantt Guard — Write protection and validation for proposals.
guardrails/inbound_filter.py | 549 | Multi-layer inbound message guardrail system.
guardrails/mcp_auth.py | 194 | MCP authentication, rate limiting, and audit logging.
guardrails/sensitivity_classifier.py | 369 | Meeting sensitivity classification — 4-tier audience system.

### core/

core/agent.py | 1060 | Main Claude agent with tool use capabilities.
core/analyst_agent.py | 124 | Analyst Agent — Accuracy-critical extraction and analysis.
core/conversation_agent.py | 192 | Conversation Agent — Handles dialogue with users via tool use.
core/cost_calculator.py | 164 | LLM cost calculator for Gianluigi.
core/dates.py | 81 | Robust date parsing for human-entered dates.
core/debrief_prompt.py | 199 | Debrief system prompts for quick injection and full debrief.
core/error_alerting.py | 98 | Error alerting for critical failures.
core/health_monitor.py | 223 | Health monitoring for Gianluigi.
core/llm.py | 222 | Centralized LLM helper for all Claude API calls.
core/logging_config.py | 90 | Structured logging configuration for Gianluigi.
core/operator_agent.py | 43 | Operator Agent — Executes write operations requiring approval.
core/retry.py | 102 | Retry decorator for transient failures.
core/router.py | 100 | Router Agent — Intent classification for incoming messages.
core/shadow_run.py | 96 | Shadow-run helpers for the v2.5 knowledge foundation.
core/system_prompt.py | 899 | Gianluigi's system prompt and personality configuration.
core/tools.py | 752 | Tool definitions for Claude API tool use.
core/weekly_review_prompt.py | 100 | System prompts for the weekly review session.

### 6 Largest Files by LOC

services/supabase_client.py | 4993 | Database CRUD, vector search, audit logging, RLS
services/telegram_bot.py | 4899 | Telegram bot for team interaction, approval flow
services/mcp_server.py | 3025 | MCP server with 45 tools (read, write, composite)
guardrails/approval_flow.py | 2943 | Eyal approval flow, draft-submit, conversational editing
services/google_sheets.py | 2170 | Google Sheets API for Tasks, Stakeholder Tracker
services/gantt_manager.py | 1294 | Gantt read/write operations, proposals, snapshots
