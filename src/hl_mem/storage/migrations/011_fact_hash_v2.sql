-- Register the fact_hash v2 migration. Python performs the data backfill
-- after this marker is recorded so JSON values can be decoded safely.
SELECT 1;
