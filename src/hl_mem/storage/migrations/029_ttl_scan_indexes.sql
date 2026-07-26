CREATE INDEX IF NOT EXISTS idx_claims_expires_scan
ON claims(status, expires_at)
WHERE expires_at IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_claims_temporal_cleanup
ON claims(status, scope, volatility, expires_at);
