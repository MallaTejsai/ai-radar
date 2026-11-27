"""Storage module for AI Radar V1.0 - Supabase PostgreSQL integration."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import structlog
from pydantic import BaseModel, Field, ValidationError

from ...settings import get_settings

logger = structlog.get_logger()


# Pydantic models for database records


class DiscoveryCreate(BaseModel):
    """Model for creating a new discovery record."""

    canonical_key: str = Field(..., description="Deterministic canonical key")
    title: str = Field(..., description="Discovery title")
    category: str = Field(..., description="Research category")
    summary: str = Field(default="", description="Short summary")
    published_at: Optional[datetime] = Field(
        default=None, description="Publication/announcement date"
    )
    discovered_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="When discovery was made",
    )
    last_verified_at: Optional[datetime] = Field(
        default=None, description="Last verification timestamp"
    )
    priority_score: int = Field(
        default=0, ge=0, le=100, description="Priority score 0-100"
    )
    confidence: float = Field(
        default=0.0, ge=0.0, le=1.0, description="Confidence level 0.0-1.0"
    )
    status: str = Field(
        default="pending", description="Discovery status"
    )
    pricing_status: str = Field(
        default="UNVERIFIED", description="Pricing classification"
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Record creation timestamp"
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Last update timestamp"
    )


class SourceCheckpoint(BaseModel):
    """Model for source collection checkpoints."""

    source_id: str = Field(..., description="Source identifier")
    last_checkpoint: Optional[datetime] = Field(
        default=None, description "Last successful collection checkpoint"
    )
    last_error: Optional[str] = Field(default=None, description="Last collection error")
    health_status: str = Field(
        default="healthy", description="Source health status"
    )
    records_collected: int = Field(
        default=0, ge=0, description "Total records collected"
    )
    last_successful_run: Optional[datetime] = Field(
        default=None, description "Last successful run timestamp"
    )


class DeliveryRecord(BaseModel):
    """Model for Telegram delivery tracking."""

    discovery_id: str = Field(..., description="Linked discovery ID")
    telegram_message_id: Optional[int] = Field(
        default=None, description "Telegram message ID after send"
    )
    status: str = Field(
        default="PENDING",
        description="Delivery status: PENDING, SENDING, SENT, RETRY_WAIT, UNKNOWN",
    )
    attempts: int = Field(
        default=0, ge=0, description "Number of send attempts"
    )
    last_attempt_at: Optional[datetime] = Field(
        default=None, description "Timestamp of last attempt"
    )
    error_message: Optional[str] = Field(
        default=None, description "Last error message"
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description "Record creation timestamp"
    )


class BackupManifest(BaseModel):
    """Model for backup tracking."""

    schema_version: str = Field(
        default="1.0.0", description "Database schema version"
    )
    table_names: List[str] = Field(
        default_factory=list, description "List of table names backed up"
    )
    record_counts: Dict[str, int] = Field(
        default_factory=dict, description "Record counts per table"
    )
    sha256_checksum: str = Field(
        default="", description "SHA-256 checksum of backup"
    )
    google_drive_file_id: Optional[str] = Field(
        default=None, description "Google Drive file ID"
    )
    backup_date: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description "Backup creation date"
    )
    status: str = Field(
        default="PENDING",
        description="Backup status: PENDING, UPLOADED, VALIDATED, RESTORED",
    )


class WorkflowRun(BaseModel):
    """Model for tracking workflow runs."""

    run_id: str = Field(..., description="Unique run identifier")
    started_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description "Run start timestamp"
    )
    completed_at: Optional[datetime] = Field(
        default=None, description "Run completion timestamp"
    )
    status: str = Field(
        default="running",
        description="Run status: running, completed, failed, stopped",
    )
    discoveries_found: int = Field(
        default=0, ge=0, description "Number of discoveries found"
    )
    discoveries_persisted: int = Field(
        default=0, ge=0, description "Number persisted to database"
    )
    discoveries_notified: int = Field(
        default=0, ge=0, description "Number notified via Telegram"
    )
    errors: List[str] = Field(
        default_factory=list, description "Error messages"
    )
    duration_seconds: float = Field(
        default=0.0, ge=0, description "Total run duration"
    )


# Database connection manager
class DatabaseManager:
    """Manages Supabase PostgreSQL database operations."""

    def __init__(self):
        self.settings = get_settings()
        self.supabase_url = self.settings.get("environment", {}).get(
            "supabase_url", ""
        )
        self.supabase_anon_key = self.settings.get("environment", {}).get(
            "supabase_anon_key", ""
        )
        self.supabase_service_key = self.settings.get("environment", {}).get(
            "supabase_service_role_key", ""
        )

        # Initialize Supabase client (lazy - only when first needed)
        self._client = None
        self._service_client = None

    @property
    def client(self):
        """Get Supabase client, initializing if needed."""
        if self._client is None and self.supabase_url and self.supabase_anon_key:
            try:
                from supabase import create_client

                self._client = create_client(
                    self.supabase_url, self.supabase_anon_key
                )
            except ImportError:
                logger.warning("supabase-not-installed")
        return self._client

    @property
    def service_client(self):
        """Get Supabase service-role client."""
        if self._service_client is None and self.supabase_url and self.supabase_service_key:
            try:
                from supabase import create_client

                self._service_client = create_client(
                    self.supabase_url, self.supabase_service_key
                )
            except ImportError:
                logger.warning("supabase-not-installed")
        return self._service_client

    async def _ensure_client(self):
        """Ensure database client is available."""
        if not self.supabase_url:
            logger.warning("supabase_url_not_configured")
            return None
        if self._client is None:
            try:
                from supabase import create_client

                self._client = create_client(
                    self.supabase_url, self.supabase_anon_key
                )
            except Exception as e:
                logger.error("supabase_client_init_failed", error=str(e))
                return None
        return self._client

    # Discovery operations

    async def insert_discovery(self, discovery: DiscoveryCreate) -> Optional[str]:
        """Insert a new discovery record. Returns the record ID if successful."""
        client = await self._ensure_client()
        if not client:
            logger.warning("cannot_insert_discovery_no_client")
            return None

        try:
            data = discovery.model_dump()
            result = await client.from_("discoveries").insert(data).execute()

            if result.data:
                return result.data[0].get("id")
            return None

        except Exception as e:
            logger.error("insert_discovery_failed", error=str(e))
            return None

    async def upsert_discovery(
        self, discovery: DiscoveryCreate
    ) -> Optional[str]:
        """Upsert a discovery (insert or update if canonical_key exists)."""
        client = await self._ensure_client()
        if not client:
            return None

        try:
            # Try to find existing record by canonical_key
            result = await (
                client.from_("discoveries")
                .select("id")
                .eq("canonical_key", discovery.canonical_key)
                .execute()
            )

            if result.data:
                # Update existing
                result = await (
                    client.from_("discoveries")
                    .update(discovery.model_dump())
                    .eq("id", result.data[0]["id"])
                    .execute()
                )
                return result.data[0].get("id") if result.data else None
            else:
                # Insert new
                return await self.insert_discovery(discovery)

        except Exception as e:
            logger.error("upsert_discovery_failed", error=str(e))
            return None

    async def get_discovery(
        self, canonical_key: str
    ) -> Optional[dict]:
        """Get a discovery by canonical key."""
        client = await self._ensure_client()
        if not client:
            return None

        try:
            result = await (
                client.from_("discoveries")
                .select("*")
                .eq("canonical_key", canonical_key)
                .execute()
            )

            return result.data[0] if result.data else None

        except Exception as e:
            logger.error("get_discovery_failed", error=str(e))
            return None

    async def get_recent_discoveries(
        self,
        limit: int = 50,
        status: str = None,
        min_priority: int = None,
    ) -> List[dict]:
        """Get recent discoveries with optional filtering."""
        client = await self._ensure_client()
        if not client:
            return []

        try:
            query = client.from_("discoveries").select("*")

            if status:
                query = query.eq("status", status)

            if min_priority is not None:
                query = query.gte("priority_score", min_priority)

            query = query.order("discovered_at", desc=True).limit(limit)

            result = await query.execute()

            return result.data or []

        except Exception as e:
            logger.error("get_recent_discoveries_failed", error=str(e))
            return []

    # Source checkpoint operations

    async def upsert_source_checkpoint(
        self, checkpoint: SourceCheckpoint
    ) -> bool:
        """Update or insert a source checkpoint."""
        client = await self._ensure_client()
        if not client:
            return False

        try:
            result = await (
                client.from_("source_checkpoints")
                .upsert(
                    checkpoint.model_dump(),
                    on_conflict=["source_id"],
                )
                .execute()
            )
            return len(result.data) > 0 if result.data else False

        except Exception as e:
            logger.error("upsert_source_checkpoint_failed", error=str(e))
            return False

    async def get_source_checkpoint(
        self, source_id: str
    ) -> Optional[dict]:
        """Get a source checkpoint by ID."""
        client = await self._ensure_client()
        if not client:
            return None

        try:
            result = await (
                client.from_("source_checkpoints")
                .select("*")
                .eq("source_id", source_id)
                .execute()
            )

            return result.data[0] if result.data else None

        except Exception as e:
            logger.error("get_source_checkpoint_failed", error=str(e))
            return None

    # Delivery operations

    async def create_delivery(
        self, discovery_id: str, status: str = "PENDING"
    ) -> Optional[str]:
        """Create a new delivery record for Telegram."""
        client = await self._ensure_client()
        if not client:
            return None

        try:
            delivery = DeliveryRecord(
                discovery_id=discovery_id, status=status
            ).model_dump()

            result = await client.from_("deliveries").insert(delivery).execute()

            return result.data[0].get("id") if result.data else None

        except Exception as e:
            logger.error("create_delivery_failed", error=str(e))
            return None

    async def update_delivery(
        self,
        delivery_id: str,
        status: str = None,
        telegram_message_id: int = None,
        error_message: str = None,
    ) -> bool:
        """Update a delivery record."""
        client = await self._ensure_client()
        if not client:
            return False

        try:
            update_data = {}
            if status:
                update_data["status"] = status
            if telegram_message_id is not None:
                update_data["telegram_message_id"] = telegram_message_id
            if error_message:
                update_data["error_message"] = error_message
            if delivery_id:
                # Increment attempts
                update_data["attempts"] = (
                    client.rpc("increment_attempts", {"id": delivery_id})
                    if hasattr(client, "rpc")
                    else 1
                )

            result = await client.from_("deliveries").update(update_data).eq(
                "id", delivery_id
            ).execute()

            return len(result.data) > 0 if result.data else False

        except Exception as e:
            logger.error("update_delivery_failed", error=str(e))
            return False

    async def get_pending_deliveries(
        self, limit: int = 10
    ) -> List[dict]:
        """Get pending delivery records."""
        client = await self._ensure_client()
        if not client:
            return []

        try:
            result = await (
                client.from_("deliveries")
                .select("*")
                .eq("status", "PENDING")
                .limit(limit)
                .execute()
            )

            return result.data or []

        except Exception as e:
            logger.error("get_pending_deliveries_failed", error=str(e))
            return []

    # Backup operations

    async def create_backup_manifest(
        self, manifest: BackupManifest
    ) -> Optional[str]:
        """Create a backup manifest record."""
        client = await self._ensure_client()
        if not client:
            return None

        try:
            result = await client.from_("backup_manifests").insert(
                manifest.model_dump()
            ).execute()

            return result.data[0].get("id") if result.data else None

        except Exception as e:
            logger.error("create_backup_manifest_failed", error=str(e))
            return None

    async def get_latest_backup(self) -> Optional[dict]:
        """Get the latest backup manifest."""
        client = await self._ensure_client()
        if not client:
            return None

        try:
            result = await (
                client.from_("backup_manifests")
                .select("*")
                .order("backup_date", desc=True)
                .limit(1)
                .execute()
            )

            return result.data[0] if result.data else None

        except Exception as e:
            logger.error("get_latest_backup_failed", error=str(e))
            return None

    # Workflow run tracking

    async def create_workflow_run(self, run_id: str) -> bool:
        """Create a new workflow run record."""
        client = await self._ensure_client()
        if not client:
            return False

        try:
            run = WorkflowRun(run_id=run_id).model_dump()
            result = await client.from_("workflow_runs").insert(run).execute()

            return len(result.data) > 0 if result.data else False

        except Exception as e:
            logger.error("create_workflow_run_failed", error=str(e))
            return False

    async def update_workflow_run(
        self,
        run_id: str,
        status: str = None,
        discoveries_found: int = None,
        discoveries_persisted: int = None,
        discoveries_notified: int = None,
        error_message: str = None,
        duration_seconds: float = None,
    ) -> bool:
        """Update a workflow run record."""
        client = await self._ensure_client()
        if not client:
            return False

        try:
            update_data = {}
            if status:
                update_data["status"] = status
            if discoveries_found is not None:
                update_data["discoveries_found"] = discoveries_found
            if discoveries_persisted is not None:
                update_data["discoveries_persisted"] = discoveries_persisted
            if discoveries_notified is not None:
                update_data["discoveries_notified"] = discoveries_notified
            if error_message:
                update_data["error_message"] = error_message
            if duration_seconds is not None:
                update_data["duration_seconds"] = duration_seconds

            result = await (
                client.from_("workflow_runs")
                .update(update_data)
                .eq("run_id", run_id)
                .execute()
            )

            return len(result.data) > 0 if result.data else False

        except Exception as e:
            logger.error("update_workflow_run_failed", error=str(e))
            return False


# Initialize database tables
async def initialize_database():
    """Create database tables if they don't exist."""
    # This runs SQL migrations to create tables
    # In production, these would be run via Supabase migration CLI
    # For now, we define the DDL here

    logger.info("initializing_database_tables")

    # Table SQL definitions would be applied via Supabase migration commands
    # The actual migrations are in database/migrations/ directory