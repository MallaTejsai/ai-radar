-- AI Radar V1.0 - free.ai provider
--
-- The original CHECK constraint listed only the three providers that existed at
-- the time. Adding a provider is a schema change, not a code-only change: without
-- this migration every free.ai analysis would fail the constraint and be lost.
--
-- Idempotent: dropping by name and re-adding is safe on a fresh or existing schema.

ALTER TABLE analysis_results
    DROP CONSTRAINT IF EXISTS analysis_results_provider_check;

ALTER TABLE analysis_results
    ADD CONSTRAINT analysis_results_provider_check
    CHECK (provider IN ('freeai', 'gemini', 'groq', 'deterministic'));
