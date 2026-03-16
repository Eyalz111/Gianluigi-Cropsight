-- Migration: Architecture Review Fixes
-- Date: 2026-03-16
-- Adds expires_at column to pending_approvals for graceful expiry

ALTER TABLE pending_approvals ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;
