-- Add optional colour per job type for UI (runsheets, timesheets, summaries).
-- Run once. If column already exists, this will error (safe to ignore on re-run).
ALTER TABLE job_types
  ADD COLUMN colour_hex VARCHAR(7) DEFAULT NULL
  COMMENT 'Hex colour e.g. #3366cc for badges/rows';
