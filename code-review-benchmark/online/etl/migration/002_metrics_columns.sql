-- Add new metric columns to llm_analyses and rename f1_score → f_beta.
-- Safe to run multiple times (IF NOT EXISTS / IF EXISTS guards).

-- New activity & performance columns
ALTER TABLE llm_analyses ADD COLUMN IF NOT EXISTS total_bot_comments INTEGER;
ALTER TABLE llm_analyses ADD COLUMN IF NOT EXISTS matched_bot_comments INTEGER;

-- Rename f1_score → f_beta (Postgres)
ALTER TABLE llm_analyses RENAME COLUMN f1_score TO f_beta;
