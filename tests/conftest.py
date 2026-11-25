"""Shared pytest fixtures."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

# A developer's .env must never change what a test asserts.
os.environ.setdefault("AI_RADAR_IGNORE_DOTENV", "1")

# psycopg's async driver cannot use the Proactor loop that is the Windows default.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

sys.path.insert(0, str(Path(__file__).parent))

from ai_radar.settings import load_config  # noqa: E402
from support.fakes import FakeRepository, FakeTelegram  # noqa: E402
from support.helpers import make_entry, make_raw, mock_client  # noqa: E402,F401 - re-exported


@pytest.fixture(scope="session")
def config():
    return load_config()


@pytest.fixture
def fake_repo():
    return FakeRepository()


@pytest.fixture
def fake_telegram():
    return FakeTelegram()
