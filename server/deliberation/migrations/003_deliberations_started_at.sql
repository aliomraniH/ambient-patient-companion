-- Add started_at column to deliberations table.
-- Required by run_progressive() which writes started_at = datetime.utcnow()
-- in the final INSERT. Safe to run multiple times (IF NOT EXISTS).
ALTER TABLE deliberations ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ DEFAULT NOW();
