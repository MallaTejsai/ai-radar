"""Typed configuration: environment secrets plus reviewed YAML policy files.

Secrets come only from the environment (GitHub encrypted secrets in production,
``.env`` locally). Everything else - weights, thresholds, alert limits - is
checked-in configuration so behaviour is reviewable and reproducible.

Loading is cached; call :func:`reset_cache` in tests.
"""

from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .models.core import Category
from .models.source import SourceRegistry
from .utils.redact import register_secret

REPO_ROOT = Path(__file__).resolve().parents[2]


class Paths(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    root: Path
    config_dir: Path
    artifacts_dir: Path


def resolve_paths(config_dir: str | Path | None = None) -> Paths:
    root = Path(os.environ.get("AI_RADAR_HOME", REPO_ROOT)).resolve()
    cfg = Path(config_dir or os.environ.get("AI_RADAR_CONFIG_DIR") or root / "config").resolve()
    artifacts = Path(os.environ.get("AI_RADAR_ARTIFACTS_DIR") or root / "artifacts").resolve()
    return Paths(root=root, config_dir=cfg, artifacts_dir=artifacts)


#: Placeholder shapes used by ``.env.example``. They must never be treated as
#: credentials: a bogus Bearer token makes GitHub answer 401 for every release
#: source, and a bogus bot token turns "not configured" into an auth error.
_PLACEHOLDER_RE = re.compile(
    r"^\s*<[^>]*>\s*$"  # <bot-token-from-botfather>
    r"|YOUR[-_]"  # YOUR-DB-PASSWORD
    r"|PROJECT-REF"  # postgres.project-ref
    r"|00000000-0000-0000-0000-000000000000",  # healthchecks example UUID
    re.IGNORECASE,
)


def looks_like_placeholder(value: str) -> bool:
    """Whether a configured value is a documented example rather than a secret."""
    if not value:
        return False
    return bool(_PLACEHOLDER_RE.search(value.strip()))


class Settings(BaseSettings):
    """Environment-backed secrets and runtime knobs."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- required for production ---
    database_url: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # --- intelligence providers (free tiers) ---
    #: free.ai (OpenAI-compatible). 30K tokens/day on the free plan, 10 req/min.
    freeai_api_key: str = ""
    freeai_model: str = "qwen7b"
    freeai_base_url: str = "https://api.free.ai"
    freeai_daily_token_budget: int = Field(default=25_000, ge=1_000, le=30_000)
    freeai_max_calls_per_run: int = Field(default=2, ge=1, le=10)
    freeai_max_output_tokens: int = Field(default=800, ge=100, le=2_000)
    gemini_api_key: str = ""
    groq_api_key: str = ""
    #: xAI Grok (OpenAI-compatible). PAID provider: every call incurs cost.
    #: Disabled unless XAI_API_KEY is set AND the owner approves usage.
    xai_api_key: str = ""
    xai_model: str = ""
    xai_base_url: str = "https://api.x.ai"
    xai_daily_token_budget: int = Field(default=5_000, ge=100, le=1_000_000)
    xai_max_calls_per_run: int = Field(default=1, ge=1, le=5)
    xai_max_output_tokens: int = Field(default=800, ge=100, le=2_000)

    #: Sarvam AI (OpenAI-compatible chat). Paid (credits).
    sarvam_api_key: str = ""
    sarvam_model: str = "sarvam-105b"
    sarvam_base_url: str = "https://api.sarvam.ai"
    sarvam_daily_token_budget: int = Field(default=5_000, ge=100, le=1_000_000)
    sarvam_max_calls_per_run: int = Field(default=1, ge=1, le=5)
    sarvam_max_output_tokens: int = Field(default=800, ge=100, le=2_000)
    #: Optional model pins. Empty means "resolve from the live API" (recommended).
    gemini_model: str = ""
    groq_model: str = ""

    # --- optional ---
    github_token: str = ""
    healthchecks_ping_url: str = ""

    # --- backup (Google Drive, OAuth2 refresh token) ---
    google_oauth_client_id: str = ""
    google_oauth_client_secret: str = ""
    google_oauth_refresh_token: str = ""
    google_drive_folder_id: str = ""
    restore_database_url: str = ""

    # --- staging safety mode ---
    #: ``AI_RADAR_STAGING=1`` validates the whole pipeline against a disposable
    #: database with every external side effect switched off. See
    #: docs/staging-validation.md. Staging mode reads ``staging_database_url``
    #: instead of ``database_url``, so it can never fall back to production.
    ai_radar_staging: bool = False
    #: Dedicated staging DSN. Deliberately NOT ``DATABASE_URL``: staging mode never
    #: reads the production URL, so it cannot fall back to it.
    ai_radar_staging_database_url: str = ""
    #: Explicit opt-in to allow Telegram delivery during a staging run. Staging
    #: database isolation remains mandatory; this only un-blanks the already
    #: verified bot token/chat id so the existing DeliveryDispatcher can send at
    #: most one real alert. Never enables any other external integration.
    ai_radar_staging_allow_telegram: bool = False

    # --- runtime knobs ---
    http_timeout_seconds: float = Field(default=20.0, ge=1.0, le=120.0)
    http_max_bytes: int = Field(default=2_000_000, ge=10_000, le=20_000_000)
    http_retries: int = Field(default=3, ge=1, le=6)
    collect_concurrency: int = Field(default=4, ge=1, le=12)
    verify_concurrency: int = Field(default=4, ge=1, le=12)
    ai_concurrency: int = Field(default=2, ge=1, le=8)
    max_alerts_per_run: int = Field(default=5, ge=1, le=10)
    max_analyses_per_run: int = Field(default=40, ge=1, le=200)
    alert_max_age_hours: int = Field(default=48, ge=1, le=720)
    recovery_window_hours: int = Field(default=24, ge=1, le=72)
    max_age_days: int = Field(default=60, ge=1, le=365)
    stale_after_minutes: int = Field(default=150, ge=30, le=1440)
    run_lock_timeout_seconds: int = Field(default=20, ge=1, le=300)
    dry_run: bool = False

    @model_validator(mode="after")
    def _drop_placeholder_credentials(self) -> "Settings":
        """Treat copied ``.env.example`` values as "not configured".

        ``.env.example`` documents itself as containing placeholders that the
        system ignores. Copying it to ``.env`` must therefore not make the app
        send ``Authorization: Bearer <github-read-only-pat>`` to GitHub: doing so
        returns 401 for all five release sources and looks like an outage rather
        than a configuration gap.
        """
        for name in (
            "database_url",
            "telegram_bot_token",
            "telegram_chat_id",
            "freeai_api_key",
            "gemini_api_key",
            "groq_api_key",
            "xai_api_key",
            "sarvam_api_key",
            "github_token",
            "healthchecks_ping_url",
            "google_oauth_client_id",
            "google_oauth_client_secret",
            "google_oauth_refresh_token",
            "google_drive_folder_id",
            "restore_database_url",
        ):
            value = getattr(self, name)
            if isinstance(value, str) and looks_like_placeholder(value):
                setattr(self, name, "")
        return self

    @model_validator(mode="after")
    def _apply_staging_guards(self) -> "Settings":
        """Structurally disable every external side effect in staging mode.

        The disposable-database check (``assert_staging_target``) stops a staging
        run from *writing* to production. This validator stops it from doing
        anything else outside staging, even if real credentials happen to be in
        ``.env``:

        * no Telegram token or chat id -> the dispatcher is never constructed;
        * no Gemini/Groq keys -> the router cannot call them (free.ai only);
        * no heartbeat URL -> monitoring is not polluted by staging runs;
        * no Drive credentials -> a staging backup can never upload into the
          production folder, whose retention would delete real backups.
        """
        if not self.ai_radar_staging:
            return self
        blank = [
            "gemini_api_key",
            "groq_api_key",
            "xai_api_key",
            "healthchecks_ping_url",
            "google_oauth_client_id",
            "google_oauth_client_secret",
            "google_oauth_refresh_token",
            "google_drive_folder_id",
        ]
        if not self.ai_radar_staging_allow_telegram:
            blank = ["telegram_bot_token", "telegram_chat_id"] + blank
        for name in blank:
            setattr(self, name, "")
        return self

    def missing_for(self, feature: Literal["research", "notify", "backup", "heartbeat", "ai"]) -> list[str]:
        """Return the environment variables a feature still needs."""
        required: dict[str, list[tuple[str, str]]] = {
            "research": [("DATABASE_URL", self.database_url)],
            "notify": [
                ("TELEGRAM_BOT_TOKEN", self.telegram_bot_token),
                ("TELEGRAM_CHAT_ID", self.telegram_chat_id),
            ],
            "backup": [
                ("GOOGLE_OAUTH_CLIENT_ID", self.google_oauth_client_id),
                ("GOOGLE_OAUTH_CLIENT_SECRET", self.google_oauth_client_secret),
                ("GOOGLE_OAUTH_REFRESH_TOKEN", self.google_oauth_refresh_token),
            ],
            "heartbeat": [("HEALTHCHECKS_PING_URL", self.healthchecks_ping_url)],
            "ai": [("GEMINI_API_KEY", self.gemini_api_key)],
        }
        return [name for name, value in required[feature] if not value]

    def register_secrets(self) -> None:
        """Register every secret with the redactor before any logging happens."""
        for value in (
            self.database_url,
            self.telegram_bot_token,
            self.freeai_api_key,
            self.gemini_api_key,
            self.groq_api_key,
            self.xai_api_key,
            self.sarvam_api_key,
            self.github_token,
            self.google_oauth_client_secret,
            self.google_oauth_refresh_token,
            self.google_oauth_client_id,
            self.healthchecks_ping_url,
            self.restore_database_url,
        ):
            register_secret(value)
        if self.telegram_bot_token and ":" in self.telegram_bot_token:
            register_secret(self.telegram_bot_token.split(":", 1)[1])


class WeightsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    technical_significance: float = Field(default=30, ge=0)
    personal_usefulness: float = Field(default=25, ge=0)
    availability_accessibility: float = Field(default=20, ge=0)
    novelty: float = Field(default=15, ge=0)
    evidence_quality: float = Field(default=10, ge=0)

    @model_validator(mode="after")
    def _must_total_100(self) -> "WeightsConfig":
        total = (
            self.technical_significance
            + self.personal_usefulness
            + self.availability_accessibility
            + self.novelty
            + self.evidence_quality
        )
        if abs(total - 100.0) > 0.01:
            raise ValueError(f"priority weights must total 100, got {total}")
        return self


class AlertConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_priority_score: int = Field(default=40, ge=0, le=100)
    min_confidence: float = Field(default=0.55, ge=0.0, le=1.0)
    min_confidence_for_digest: float = Field(default=0.55, ge=0.0, le=1.0)
    max_alerts_per_run: int = Field(default=5, ge=1, le=10)


class ConfidenceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    discard_below: float = Field(default=0.55, ge=0.0, le=1.0)
    calibration_file: str = "confidence_thresholds.json"


class SignalsConfig(BaseModel):
    """Keyword signals used by the deterministic scorer/classifier."""

    model_config = ConfigDict(extra="forbid")

    major_release: list[str] = Field(default_factory=list)
    minor_update: list[str] = Field(default_factory=list)
    capability: list[str] = Field(default_factory=list)
    free_signal: list[str] = Field(default_factory=list)
    open_source_signal: list[str] = Field(default_factory=list)
    paid_signal: list[str] = Field(default_factory=list)
    free_trial_signal: list[str] = Field(default_factory=list)
    deprecation_signal: list[str] = Field(default_factory=list)


class ScoringConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    weights: WeightsConfig = Field(default_factory=WeightsConfig)
    alert: AlertConfig = Field(default_factory=AlertConfig)
    confidence: ConfidenceConfig = Field(default_factory=ConfidenceConfig)
    signals: SignalsConfig = Field(default_factory=SignalsConfig)
    category_usefulness: dict[Category, float] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _usefulness_bounds(self) -> "ScoringConfig":
        for key, value in self.category_usefulness.items():
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"category_usefulness[{key}] must be within 0..1")
        return self


class CategorySpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str
    description: str = ""
    subcategories: list[str] = Field(default_factory=list)


class CategoriesConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    categories: dict[Category, CategorySpec]


class Calibration(BaseModel):
    """Result of the labelled-threshold evaluation, if it has been run."""

    model_config = ConfigDict(extra="ignore")

    calibrated: bool = False
    threshold: float = 0.55
    n_samples: int = 0
    precision: float | None = None
    recall: float | None = None
    generated_at: str | None = None
    dataset: str | None = None
    note: str = "uncalibrated - using the configured default threshold"


class ScheduleEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cron_utc: str
    window_ist_start: str = ""
    window_ist_end: str = ""
    note: str = ""


class SchedulePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    research: ScheduleEntry
    digest: ScheduleEntry
    backup: ScheduleEntry
    maintenance: ScheduleEntry


class LimitsPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_alerts_per_run: int = Field(default=5, ge=1, le=10)
    recovery_window_hours: int = Field(default=24, ge=1, le=72)
    max_analyses_per_run: int = Field(default=40, ge=1, le=200)
    alert_max_age_hours: int = Field(default=48, ge=1, le=720)
    stale_after_minutes: int = Field(default=150, ge=30, le=1440)


class Policy(BaseModel):
    """Non-secret deployment policy from ``config/settings.yaml``."""

    model_config = ConfigDict(extra="forbid")

    schedule: SchedulePolicy
    limits: LimitsPolicy


class AppConfig(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    settings: Settings
    sources: SourceRegistry
    scoring: ScoringConfig
    categories: CategoriesConfig
    policy: Policy
    calibration: Calibration
    paths: Paths

    @property
    def confidence_threshold(self) -> float:
        """Operating confidence threshold - calibrated when available."""
        if self.calibration.calibrated:
            return self.calibration.threshold
        return self.scoring.confidence.discard_below

    @property
    def min_priority(self) -> int:
        return self.scoring.alert.min_priority_score

    @property
    def max_alerts_per_run(self) -> int:
        """Alert cap: the strictest of policy file, scoring file and runtime env."""
        return min(
            self.policy.limits.max_alerts_per_run,
            self.scoring.alert.max_alerts_per_run,
            self.settings.max_alerts_per_run,
        )

    @property
    def recovery_window_hours(self) -> int:
        return self.policy.limits.recovery_window_hours

    @property
    def alert_max_age_hours(self) -> int:
        return self.policy.limits.alert_max_age_hours


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"missing configuration file: {path}")
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return data


def _read_calibration(config_dir: Path, file_name: str) -> Calibration:
    path = config_dir / file_name
    if not path.exists():
        return Calibration()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return Calibration(note="calibration file unreadable - using default threshold")
    payload.setdefault("calibrated", True)
    return Calibration.model_validate(payload)


def _load_settings() -> Settings:
    """Build Settings from the environment, optionally ignoring a local .env.

    Tests set AI_RADAR_IGNORE_DOTENV=1 so a developer's real .env cannot leak into
    a test that asserts "no database is configured". Production never sets it.
    """
    if os.environ.get("AI_RADAR_IGNORE_DOTENV") == "1":
        return Settings(_env_file=None)
    return Settings()


@lru_cache(maxsize=1)
def load_config(config_dir: str | None = None) -> AppConfig:
    """Load and validate the whole configuration surface exactly once."""
    paths = resolve_paths(config_dir)
    settings = _load_settings()
    settings.register_secrets()

    sources = SourceRegistry.model_validate(_read_yaml(paths.config_dir / "sources.yaml"))
    problems = sources.validate_registry()
    if problems:
        raise ValueError("invalid source registry: " + "; ".join(problems))

    scoring = ScoringConfig.model_validate(_read_yaml(paths.config_dir / "scoring.yaml"))
    categories = CategoriesConfig.model_validate(_read_yaml(paths.config_dir / "categories.yaml"))
    policy = Policy.model_validate(_read_yaml(paths.config_dir / "settings.yaml"))
    calibration = _read_calibration(paths.config_dir, scoring.confidence.calibration_file)

    return AppConfig(
        settings=settings,
        sources=sources,
        scoring=scoring,
        categories=categories,
        policy=policy,
        calibration=calibration,
        paths=paths,
    )


def reset_cache() -> None:
    load_config.cache_clear()
