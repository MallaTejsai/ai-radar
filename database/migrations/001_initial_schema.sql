-- AI Radar V1.0 - initial schema (Supabase PostgreSQL)
--
-- Idempotent by construction: every object uses IF NOT EXISTS and no ALTER TABLE
-- ADD CONSTRAINT is used, because a re-run must never fail on a live database.
-- All timestamps are timestamptz (UTC). Money-like values do not exist here.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ---------------------------------------------------------------- migrations
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     text PRIMARY KEY,
    applied_at  timestamptz NOT NULL DEFAULT now(),
    checksum    text NOT NULL
);

-- ------------------------------------------------------------------- sources
-- Operational state for every entry of config/sources.yaml. The YAML file is the
-- reviewed registry; this table holds checkpoint/health so state survives runners.
CREATE TABLE IF NOT EXISTS sources (
    source_id             text PRIMARY KEY,
    name                  text NOT NULL,
    category              text NOT NULL,
    tier                  char(1) NOT NULL CHECK (tier IN ('A', 'B', 'C')),
    method                text NOT NULL CHECK (method IN ('rss', 'github_releases', 'huggingface_models')),
    url                   text NOT NULL,
    enabled               boolean NOT NULL DEFAULT true,
    health_status         text NOT NULL DEFAULT 'unknown'
                              CHECK (health_status IN ('unknown', 'healthy', 'degraded', 'failing', 'disabled')),
    consecutive_failures  integer NOT NULL DEFAULT 0 CHECK (consecutive_failures >= 0),
    last_checkpoint       timestamptz,
    last_success_at       timestamptz,
    last_error            text,
    etag                  text,
    last_modified         text,
    records_collected     bigint NOT NULL DEFAULT 0 CHECK (records_collected >= 0),
    created_at            timestamptz NOT NULL DEFAULT now(),
    updated_at            timestamptz NOT NULL DEFAULT now()
);

-- --------------------------------------------------------------- discoveries
CREATE TABLE IF NOT EXISTS discoveries (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    canonical_key     text NOT NULL UNIQUE,
    title_fingerprint text NOT NULL,
    title             text NOT NULL,
    category          text NOT NULL,
    summary           text NOT NULL DEFAULT '',
    url               text NOT NULL DEFAULT '',
    source_id         text NOT NULL REFERENCES sources (source_id) ON DELETE RESTRICT,
    tier              char(1) NOT NULL CHECK (tier IN ('A', 'B', 'C')),
    published_at      timestamptz,
    discovered_at     timestamptz NOT NULL DEFAULT now(),
    last_verified_at  timestamptz,
    priority_score    integer NOT NULL DEFAULT 0 CHECK (priority_score BETWEEN 0 AND 100),
    priority_label    text NOT NULL DEFAULT '1/5',
    confidence        double precision NOT NULL DEFAULT 0 CHECK (confidence >= 0 AND confidence <= 1),
    status            text NOT NULL DEFAULT 'verified'
                          CHECK (status IN ('verified', 'notified', 'archived')),
    pricing_status    text NOT NULL DEFAULT 'UNVERIFIED'
                          CHECK (pricing_status IN ('FREE', 'FREE_TIER', 'OPEN_SOURCE', 'PAID', 'UNVERIFIED')),
    pricing_restricted boolean NOT NULL DEFAULT false,
    verdict           text NOT NULL DEFAULT 'INSUFFICIENT'
                          CHECK (verdict IN ('CONFIRMED', 'PARTIAL', 'INSUFFICIENT', 'DISPROVED', 'ERROR')),
    score_breakdown   jsonb,
    verification      jsonb,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_discoveries_category_priority
    ON discoveries (category, priority_score DESC);
CREATE INDEX IF NOT EXISTS idx_discoveries_discovered_at
    ON discoveries (discovered_at DESC);
CREATE INDEX IF NOT EXISTS idx_discoveries_title_fingerprint
    ON discoveries (title_fingerprint);
CREATE INDEX IF NOT EXISTS idx_discoveries_status_verdict
    ON discoveries (status, verdict);

-- -------------------------------------------------------- discovery_sources
-- Evidence rows. One discovery can be attested by several sources; this is what
-- makes cross-source corroboration measurable instead of assumed.
CREATE TABLE IF NOT EXISTS discovery_sources (
    discovery_id uuid NOT NULL REFERENCES discoveries (id) ON DELETE CASCADE,
    source_id    text NOT NULL REFERENCES sources (source_id) ON DELETE RESTRICT,
    evidence_url text NOT NULL,
    tier         char(1) NOT NULL CHECK (tier IN ('A', 'B', 'C')),
    domain       text NOT NULL DEFAULT '',
    http_status  integer,
    reachable    boolean,
    checked_at   timestamptz,
    created_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (discovery_id, source_id, evidence_url)
);

CREATE INDEX IF NOT EXISTS idx_discovery_sources_source
    ON discovery_sources (source_id);

-- ---------------------------------------------------------- analysis_results
CREATE TABLE IF NOT EXISTS analysis_results (
    id                bigserial PRIMARY KEY,
    discovery_id      uuid NOT NULL REFERENCES discoveries (id) ON DELETE CASCADE,
    provider          text NOT NULL CHECK (provider IN ('gemini', 'groq', 'deterministic')),
    model_id          text NOT NULL DEFAULT '',
    prompt_version    text NOT NULL,
    input_fingerprint text NOT NULL,
    payload           jsonb NOT NULL,
    latency_ms        integer NOT NULL DEFAULT 0 CHECK (latency_ms >= 0),
    cached            boolean NOT NULL DEFAULT false,
    fallback_reason   text,
    created_at        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (discovery_id, prompt_version)
);

-- ----------------------------------------------------------- pricing_history
CREATE TABLE IF NOT EXISTS pricing_history (
    id             bigserial PRIMARY KEY,
    discovery_id   uuid NOT NULL REFERENCES discoveries (id) ON DELETE CASCADE,
    pricing_status text NOT NULL,
    restricted     boolean NOT NULL DEFAULT false,
    evidence       text NOT NULL DEFAULT '',
    observed_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_pricing_history_discovery
    ON pricing_history (discovery_id, observed_at DESC);

-- ---------------------------------------------------------------- deliveries
-- Persistent delivery intent. Written before the network call so a crash after
-- Telegram accepted a message leaves an UNKNOWN row rather than silence.
CREATE TABLE IF NOT EXISTS deliveries (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    discovery_id        uuid NOT NULL REFERENCES discoveries (id) ON DELETE CASCADE,
    channel             text NOT NULL DEFAULT 'telegram' CHECK (channel IN ('telegram')),
    status              text NOT NULL DEFAULT 'PENDING'
                            CHECK (status IN ('PENDING', 'SENDING', 'SENT', 'RETRY_WAIT', 'UNKNOWN')),
    attempts            integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    message_text        text NOT NULL,
    message_hash        text NOT NULL,
    telegram_message_id bigint,
    last_error          text,
    claimed_by          text,
    claimed_at          timestamptz,
    next_attempt_at     timestamptz NOT NULL DEFAULT now(),
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (discovery_id, channel)
);

CREATE INDEX IF NOT EXISTS idx_deliveries_dispatchable
    ON deliveries (status, next_attempt_at);

-- ------------------------------------------------------------ workflow_runs
CREATE TABLE IF NOT EXISTS workflow_runs (
    run_id               text PRIMARY KEY,
    started_at           timestamptz NOT NULL DEFAULT now(),
    finished_at          timestamptz,
    status               text NOT NULL DEFAULT 'running'
                             CHECK (status IN ('running', 'completed', 'failed', 'skipped_locked', 'degraded')),
    trigger              text NOT NULL DEFAULT 'schedule',
    sources_attempted    integer NOT NULL DEFAULT 0,
    sources_failed       integer NOT NULL DEFAULT 0,
    entries_collected    integer NOT NULL DEFAULT 0,
    after_dedup          integer NOT NULL DEFAULT 0,
    verified             integer NOT NULL DEFAULT 0,
    qualified            integer NOT NULL DEFAULT 0,
    persisted_new        integer NOT NULL DEFAULT 0,
    alerts_sent          integer NOT NULL DEFAULT 0,
    recovered_missed_runs integer NOT NULL DEFAULT 0,
    duration_seconds     double precision NOT NULL DEFAULT 0,
    report               jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_workflow_runs_started
    ON workflow_runs (started_at DESC);
CREATE INDEX IF NOT EXISTS idx_workflow_runs_status
    ON workflow_runs (status, started_at DESC);

-- ------------------------------------------------------------- daily_digests
CREATE TABLE IF NOT EXISTS daily_digests (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    digest_date         date NOT NULL UNIQUE,
    digest_text         text NOT NULL,
    discovery_count     integer NOT NULL DEFAULT 0,
    discovery_keys      jsonb NOT NULL DEFAULT '[]'::jsonb,
    telegram_message_id bigint,
    created_at          timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------- backup_manifests
CREATE TABLE IF NOT EXISTS backup_manifests (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    backup_id          text NOT NULL UNIQUE,
    schema_version     text NOT NULL,
    created_at         timestamptz NOT NULL DEFAULT now(),
    table_counts       jsonb NOT NULL DEFAULT '{}'::jsonb,
    file_name          text NOT NULL,
    uncompressed_bytes bigint NOT NULL DEFAULT 0,
    compressed_bytes   bigint NOT NULL DEFAULT 0,
    sha256             text NOT NULL,
    drive_file_id      text,
    status             text NOT NULL DEFAULT 'PENDING'
                           CHECK (status IN ('PENDING', 'UPLOADED', 'VALIDATED', 'RESTORED', 'FAILED')),
    validated_at       timestamptz,
    restore_tested_at  timestamptz,
    notes              jsonb NOT NULL DEFAULT '[]'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_backup_manifests_created
    ON backup_manifests (created_at DESC);

-- ------------------------------------------------------------- updated_at
CREATE OR REPLACE FUNCTION ai_radar_touch_updated_at()
RETURNS trigger AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_sources_updated_at ON sources;
CREATE TRIGGER trg_sources_updated_at
    BEFORE UPDATE ON sources
    FOR EACH ROW EXECUTE FUNCTION ai_radar_touch_updated_at();

DROP TRIGGER IF EXISTS trg_discoveries_updated_at ON discoveries;
CREATE TRIGGER trg_discoveries_updated_at
    BEFORE UPDATE ON discoveries
    FOR EACH ROW EXECUTE FUNCTION ai_radar_touch_updated_at();

DROP TRIGGER IF EXISTS trg_deliveries_updated_at ON deliveries;
CREATE TRIGGER trg_deliveries_updated_at
    BEFORE UPDATE ON deliveries
    FOR EACH ROW EXECUTE FUNCTION ai_radar_touch_updated_at();
