-- AI Radar V1.0 - provider CHECK constraint (grok + sarvam)
--
-- Migration 003 added free.ai. The provider adapters for xAI Grok and Sarvam AI
-- were added later and their analysis results would violate the existing CHECK
-- constraint, silently losing every Grok/Sarvam analysis at persist time.
--
-- This migration extends the allowed provider set without touching any data.
-- Idempotent: drop by name and re-add.

ALTER TABLE analysis_results
    DROP CONSTRAINT IF EXISTS analysis_results_provider_check;

ALTER TABLE analysis_results
    ADD CONSTRAINT analysis_results_provider_check
    CHECK (provider IN ('freeai', 'gemini', 'groq', 'grok', 'sarvam', 'deterministic'));