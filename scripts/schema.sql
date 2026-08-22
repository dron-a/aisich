CREATE TABLE users (
    phone_number  TEXT PRIMARY KEY,          -- one row per user, enforced by PK
    api_key       TEXT NOT NULL,             -- TODO: encrypt at rest (pgcrypto)
    llm_provider  TEXT NOT NULL DEFAULT 'huggingface',
    llm_model     TEXT NOT NULL DEFAULT 'mistralai/Mistral-7B-Instruct-v0.3',
    is_active     BOOLEAN NOT NULL DEFAULT true
);

-- Needed only by the optional Cloudflare remove-by-key endpoint (path B).
CREATE INDEX idx_users_api_key ON users (api_key);