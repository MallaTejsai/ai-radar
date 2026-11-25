"""Source registry models - the curated, validated list of collection targets."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .core import Category, CollectionMethod, SourceTier


class SourceSpec(BaseModel):
    """One entry of ``config/sources.yaml``.

    The registry is code-reviewed configuration, not an open scraper list, so
    every field is required and validated.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z0-9]+(?:_[a-z0-9]+)*$", max_length=64)
    name: str = Field(min_length=2, max_length=120)
    category: Category
    tier: SourceTier
    method: CollectionMethod
    url: str = Field(min_length=8, max_length=512)
    enabled: bool = True
    max_items: int = Field(default=40, ge=1, le=100)
    #: Hub-only substance gates. Uncurated repositories are dominated by
    #: throwaway uploads, so a minimum amount of real usage is required.
    min_likes: int = Field(default=0, ge=0, le=100000)
    min_downloads: int = Field(default=0, ge=0, le=100000000)
    notes: str = Field(default="", max_length=400)

    @field_validator("url")
    @classmethod
    def _https_only(cls, value: str) -> str:
        if not value.startswith("https://"):
            raise ValueError("source URLs must use https")
        return value


class SourceState(BaseModel):
    """Mutable per-source operational state, mirrored in the ``sources`` table."""

    model_config = ConfigDict(extra="ignore")

    source_id: str
    last_checkpoint: datetime | None = None
    last_success_at: datetime | None = None
    last_error: str | None = None
    consecutive_failures: int = 0
    health_status: str = "unknown"
    records_collected: int = 0
    etag: str | None = None
    last_modified: str | None = None


class SourceRegistry(BaseModel):
    """The parsed registry document."""

    model_config = ConfigDict(extra="forbid")

    sources: list[SourceSpec]

    def by_id(self) -> dict[str, SourceSpec]:
        return {source.id: source for source in self.sources}

    def enabled(self) -> list[SourceSpec]:
        return [source for source in self.sources if source.enabled]

    def validate_registry(self) -> list[str]:
        """Return structural problems with the registry.

        Used by tests and by ``ai-radar sources --validate``. Deliberately not a
        model validator: a broken registry should fail loudly at startup, but the
        diagnostics are more useful as a list.
        """
        problems: list[str] = []
        ids = [source.id for source in self.sources]
        if len(ids) != len(set(ids)):
            problems.append("duplicate source ids present")
        if not 15 <= len(self.sources) <= 25:
            problems.append(f"registry must contain 15-25 sources, found {len(self.sources)}")
        covered = {source.category for source in self.sources}
        missing = [category.value for category in Category if category not in covered]
        if missing:
            problems.append(f"categories not covered: {', '.join(sorted(missing))}")
        if not any(source.tier is SourceTier.A for source in self.sources):
            problems.append("registry needs at least one Tier A source")
        if sum(1 for source in self.sources if source.enabled) < 10:
            problems.append("fewer than 10 enabled sources")
        return problems
