-- AI Radar V1.0 - runtime support tables
--
-- The nine tables required by the specification are in 001. These three exist
-- because real operation needs them and they are cheaper than overloading a
-- required table with unrelated responsibilities:
--
--   analysis_cache  - avoids paying for the same analysis twice (response caching)
--   provider_usage  - per-call accounting for quota and cost control
--   system_state    - heartbeat snapshot and other singleton state

CREATE TABLE IF NOT EXISTS analysis_cache (
    fingerprint    text PRIMARY KEY,
    provider       text NOT NULL,
    model_id       text NOT NULL DEFAULT '',
    prompt_version text NOT NULL,
    payload        jsonb NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_analysis_cache_created
    ON analysis_cache (created_at DESC);

CREATE TABLE IF NOT EXISTS provider_usage (
    id                bigserial PRIMARY KEY,
    occurred_at       timestamptz NOT NULL DEFAULT now(),
    provider          text NOT NULL,
    model_id          text NOT NULL DEFAULT '',
    ok                boolean NOT NULL,
    error_kind        text,
    latency_ms        integer NOT NULL DEFAULT 0 CHECK (latency_ms >= 0),
    prompt_tokens     integer,
    completion_tokens integer
);

CREATE INDEX IF NOT EXISTS idx_provider_usage_occurred
    ON provider_usage (occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_provider_usage_provider
    ON provider_usage (provider, occurred_at DESC);

CREATE TABLE IF NOT EXISTS system_state (
    key        text PRIMARY KEY,
    value      jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
