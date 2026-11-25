"""Settings and configuration loading for AI Radar V1.0."""

import os
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Any

import yaml


def load_yaml_config(path: Path) -> dict:
    """Load a YAML configuration file."""
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def get_settings() -> Dict[str, Any]:
    """Load all configuration settings from config directory."""
    base_dir = Path(__file__).parent.parent

    settings = {
        # Load from YAML configs
        "sources": load_yaml_config(base_dir / "config" / "sources.yaml"),
        "categories": load_yaml_config(base_dir / "config" / "categories.yaml"),
        "scoring": load_yaml_config(base_dir / "config" / "scoring.yaml"),
        "settings": load_yaml_config(base_dir / "config" / "settings.yaml"),

        # Environment variables with fallbacks
        "environment": {
            "github_actions": os.environ.get("GITHUB_ACTIONS", "false").lower() == "true",
            "supabase_url": os.environ.get("SUPABASE_URL", ""),
            "supabase_anon_key": os.environ.get("SUPABASE_ANON_KEY", ""),
            "supabase_service_role_key": os.environ.get("SUPABASE_SERVICE_ROLE_KEY", ""),
            "telegram_bot_token": os.environ.get("TELEGRAM_BOT_TOKEN", ""),
            "telegram_chat_id": os.environ.get("TELEGRAM_CHAT_ID", ""),
            "google_drive_folder_id": os.environ.get("GOOGLE_DRIVE_BACKUP_FOLDER_ID", ""),
            "google_drive_service_account_key": os.environ.get(
                "GOOGLE_DRIVE_SERVICE_ACCOUNT_KEY", ""
            ),
        },

        # Derived settings
        "derived": {
            "min_priority_score": 40,  # 3/5 threshold
            "max_alerts_per_hour": 5,
            "min_confidence_for_alert": 0.5,
            "ist_offset": "+05:30",
            "research_start_hour_ist": 8,
            "research_end_hour_ist": 22,
            "research_minute": 30,
        },

        # Timestamp
        "loaded_at": datetime.utcnow().isoformat() + "Z",
    }

    return settings


def get_source_config(source_id: str) -> Optional[dict]:
    """Get configuration for a specific source by ID."""
    sources = get_settings().get("sources", {})
    return sources.get("sources", {}).get(source_id)


def get_category_config(category_name: str) -> Optional[dict]:
    """Get configuration for a specific category."""
    categories = get_settings().get("categories", {})
    return categories.get("categories", {}).get(category_name)


def is_research_hour_ist(current_time_ist: datetime) -> bool:
    """Check if current time is within research hours (8:30-22:00 IST)."""
    hour = current_time_ist.hour
    minute = current_time_ist.minute
    # Research runs at minute 30 of each hour from 8 to 22
    return 8 <= hour <= 22 and minute >= 30 or (hour == 22 and minute >= 30) or hour == 8 and minute == 30