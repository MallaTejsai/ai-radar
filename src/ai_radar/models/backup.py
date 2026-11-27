"""Backup and digest models."""

from __future__ import annotations

from datetime import date, datetime, timezone

from pydantic import BaseModel, ConfigDict, Field


class BackupManifest(BaseModel):
    """Manifest committed next to every database export.

    A backup is only considered valid after the uploaded bytes were read back and
    their SHA-256 matched this value.
    """

    model_config = ConfigDict(extra="ignore")

    backup_id: str
    schema_version: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    code_version: str = "1.0.0"
    table_names: list[str] = Field(default_factory=list)
    record_counts: dict[str, int] = Field(default_factory=dict)
    file_name: str = ""
    uncompressed_bytes: int = 0
    compressed_bytes: int = 0
    sha256_checksum: str = ""
    google_drive_file_id: str | None = None
    status: str = "PENDING"
    validated_at: datetime | None = None
    restore_tested_at: datetime | None = None
    notes: list[str] = Field(default_factory=list)


class DigestContent(BaseModel):
    """The rendered daily digest plus the IDs it was built from (for de-duplication)."""

    model_config = ConfigDict(extra="ignore")

    digest_date: date
    text: str = Field(max_length=12000)
    discovery_keys: list[str] = Field(default_factory=list)
    top_count: int = 0
    honest_empty: bool = False
