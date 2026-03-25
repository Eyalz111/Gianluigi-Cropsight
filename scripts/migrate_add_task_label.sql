-- Add label column to tasks table for topic-based scanning
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS label TEXT;
