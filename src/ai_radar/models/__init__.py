"""Typed domain models for AI Radar V1.0."""

from .analysis import AnalysisPayload, AnalysisResult
from .backup import BackupManifest, DigestContent
from .core import (
    Category,
    Channel,
    CollectionMethod,
    DeliveryStatus,
    DiscoveryStatus,
    PricingStatus,
    Provider,
    RunStatus,
    SourceTier,
    Verdict,
)
from .delivery import DeliveryIntent, DispatchOutcome
from .discovery import (
    DiscoveryRecord,
    Evidence,
    NormalizedEntry,
    RawEntry,
    ScoreBreakdown,
    ScoreComponent,
    VerificationDetail,
)
from .run import HeartbeatSnapshot, RunReport
from .source import SourceRegistry, SourceSpec, SourceState

__all__ = [
    "AnalysisPayload",
    "AnalysisResult",
    "BackupManifest",
    "Category",
    "Channel",
    "CollectionMethod",
    "DeliveryIntent",
    "DeliveryStatus",
    "DigestContent",
    "DiscoveryRecord",
    "DiscoveryStatus",
    "DispatchOutcome",
    "Evidence",
    "HeartbeatSnapshot",
    "NormalizedEntry",
    "PricingStatus",
    "Provider",
    "RawEntry",
    "RunReport",
    "RunStatus",
    "ScoreBreakdown",
    "ScoreComponent",
    "SourceRegistry",
    "SourceSpec",
    "SourceState",
    "SourceTier",
    "Verdict",
    "VerificationDetail",
]
