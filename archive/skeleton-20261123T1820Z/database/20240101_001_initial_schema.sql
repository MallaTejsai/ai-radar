-- AI Radar V1.0 Initial Database Schema
-- Supabase PostgreSQL migrations

-- ============================================================
-- Extensions
-- ============================================================

CREATE EXTENSION IF NOT EXISTS "pgcrypto";
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ============================================================
-- Tables
-- ============================================================

-- discoveries table
CREATE TABLE IF NOT EXISTS discoveries (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    canonical_key VARCHAR(255) NOT NULL UNIQUE,
    title TEXT NOT NULL,
    category VARCHAR(100) NOT NULL,
    summary TEXT,
    published_at TIMESTAMP WITH TIME ZONE,
    discovered_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    last_verified_at TIMESTAMP WITH TIME ZONE,
    priority_score INTEGER CHECK (priority_score >= 0 AND priority_score <= 100),
    confidence FLOAT CHECK (confidence >= 0 AND confidence <= 1.0),
    status VARCHAR(50) NOT NULL DEFAULT 'pending',
    pricing_status VARCHAR(50) NOT NULL DEFAULT 'UNVERIFIED',
    source_id VARCHAR(100),
    url TEXT,
    evidence_urls TEXT[],  -- Array of evidence source URLs
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);

-- Create index on canonical_key for fast lookups
CREATE INDEX IF NOT EXISTS idx_discoveries_canonical_key ON discoveries(canonical_key);

-- Index on category and priority for filtering
CREATE INDEX IF NOT EXISTS idx_discoveries_category_priority ON discoveries(category, priority_score DESC);

-- Index on discovery status
CREATE INDEX IF NOT EXISTS idx_discoveries_status ON discoveries(status);

-- Index on discovered_at for time-based queries
CREATE INDEX IF NOT EXISTS idx_discoveries_discovered_at ON discoveries(discovered_at DESC);

-- verification_status can be: CONFIRMED, PARTIAL, INSUFFICIENT, ERROR, DISPROVED


-- discovery_sources table (source metadata)
CREATE TABLE IF NOT EXISTS discovery_sources (
    id SERIAL PRIMARY KEY,
    source_id VARCHAR(100) NOT NULL UNIQUE,
    name VARCHAR(255) NOT NULL,
    category VARCHAR(100) NOT NULL,
    url TEXT NOT NULL,
    url_type VARCHAR(50) NOT NULL DEFAULT 'rss',  -- rss, http_get, github_api
    tier VARCHAR(20) NOT NULL DEFAULT 'C',  -- A, B, C
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    checkpoint TIMESTAMP WITH TIME ZONE,
    last_error TEXT,
    health_status VARCHAR(50) NOT NULL DEFAULT 'healthy',
    records_collected INTEGER NOT NULL DEFAULT 0,
    last_successful_run TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);

-- Index on source_id for lookups
CREATE INDEX IF NOT EXISTS idx_discovery_sources_source_id ON discovery_sources(source_id);


-- source_checkpoints table (collection checkpoints)
CREATE TABLE IF NOT EXISTS source_checkpoints (
    id SERIAL PRIMARY KEY,
    source_id VARCHAR(100) NOT NULL UNIQUE,
    last_checkpoint TIMESTAMP WITH TIME ZONE,
    last_error TEXT,
    health_status VARCHAR(50) NOT NULL DEFAULT 'healthy',
    records_collected INTEGER NOT NULL DEFAULT 0,
    last_successful_run TIMESTAMP WITH TIME ZONE,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);

-- Index on source_id
CREATE INDEX IF NOT EXISTS idx_source_checkpoints_source_id ON source_checkpoints(source_id);


-- deliveries table (Telegram delivery tracking)
CREATE TABLE IF NOT EXISTS deliveries (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    discovery_id UUID NOT NULL REFERENCES discoveries(id) ON DELETE CASCADE,
    telegram_message_id INTEGER,
    status VARCHAR(50) NOT NULL DEFAULT 'PENDING',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_attempt_at TIMESTAMP WITH TIME ZONE,
    error_message TEXT,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);

-- Index on discovery_id for lookups
CREATE INDEX IF NOT EXISTS idx_deliveries_discovery_id ON deliveries(discovery_id);

-- Index on status for finding pending sends
CREATE INDEX IF NOT EXISTS idx_deliveries_status ON deliveries(status);

-- Check constraint for delivery status
ALTER TABLE deliveries ADD CONSTRAINT chk_deliveries_status
    CHECK (status IN ('PENDING', 'SENDING', 'SENT', 'RETRY_WAIT', 'UNKNOWN'));


-- backup_manifests table (Google Drive backup tracking)
CREATE TABLE IF NOT EXISTS backup_manifests (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    schema_version VARCHAR(50) NOT NULL DEFAULT '1.0.0',
    table_names TEXT[] NOT NULL DEFAULT '{}',
    record_counts JSONB NOT NULL DEFAULT '{}'::jsonb,
    sha256_checksum VARCHAR(64) NOT NULL DEFAULT '',
    google_drive_file_id VARCHAR(255),
    backup_date TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    status VARCHAR(50) NOT NULL DEFAULT 'PENDING',
    validated_at TIMESTAMP WITH TIME ZONE,
    restore_tested_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);

-- Index on backup_date for chronological queries
CREATE INDEX IF NOT EXISTS idx_backup_manifests_backup_date ON backup_manifests(backup_date DESC);

-- Check constraint for backup status
ALTER TABLE backup_manifests ADD CONSTRAINT chk_backup_manifests_status
    CHECK (status IN ('PENDING', 'UPLOADED', 'VALIDATED', 'RESTORED'));


-- workflow_runs table (pipeline execution tracking)
CREATE TABLE IF NOT EXISTS workflow_runs (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    run_id VARCHAR(100) NOT NULL UNIQUE,
    started_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    completed_at TIMESTAMP WITH TIME ZONE,
    status VARCHAR(50) NOT NULL DEFAULT 'running',
    discoveries_found INTEGER NOT NULL DEFAULT 0,
    discoveries_persisted INTEGER NOT NULL DEFAULT 0,
    discoveries_notified INTEGER NOT NULL DEFAULT 0,
    error_message TEXT,
    duration_seconds FLOAT NOT NULL DEFAULT 0.0,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);

-- Index on run_id
CREATE INDEX IF NOT EXISTS idx_workflow_runs_run_id ON workflow_runs(run_id);

-- Index on status for finding recent runs
CREATE INDEX IF NOT EXISTS idx_workflow_runs_status ON workflow_runs(status);


-- daily_digests table (stored digest records)
CREATE TABLE IF NOT EXISTS daily_digests (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    digest_date DATE NOT NULL DEFAULT (current_date),
    digest_text TEXT NOT NULL,
    discovery_count INTEGER NOT NULL DEFAULT 0,
    priority_minimum INTEGER NOT NULL DEFAULT 40,
    sent_via VARCHAR(50) DEFAULT 'telegram',
    telegram_message_id INTEGER,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);

-- Index on digest date
CREATE INDEX IF NOT EXISTS idx_daily_digests_date ON daily_digests(digest_date DESC);

-- Constraint on digest date (unique per day)
ALTER TABLE daily_digests ADD CONSTRAINT uk_daily_digests_date
    UNIQUE (digest_date);