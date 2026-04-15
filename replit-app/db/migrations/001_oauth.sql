-- OAuth storage tables used by replit-app/lib/oauth-store.ts.
-- Applied automatically on first pool acquisition (see ensureSchema()).
-- Idempotent via IF NOT EXISTS so repeat runs are safe.

CREATE TABLE IF NOT EXISTS oauth_clients (
  client_id     TEXT  PRIMARY KEY,
  client_secret TEXT  NOT NULL,
  redirect_uris JSONB NOT NULL,
  client_name   TEXT,
  created_at    BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS oauth_auth_codes (
  code                  TEXT  PRIMARY KEY,
  client_id             TEXT  NOT NULL,
  redirect_uri          TEXT  NOT NULL,
  code_challenge        TEXT,
  code_challenge_method TEXT,
  expires_at            BIGINT NOT NULL,
  used                  BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS oauth_access_tokens (
  token      TEXT   PRIMARY KEY,
  client_id  TEXT   NOT NULL,
  expires_at BIGINT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_oauth_access_tokens_exp ON oauth_access_tokens(expires_at);
CREATE INDEX IF NOT EXISTS idx_oauth_auth_codes_exp    ON oauth_auth_codes(expires_at);
