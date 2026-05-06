"""Shared pytest fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from memex.config import Settings, reset_settings_for_testing
from memex.core.engine import Engine


@pytest.fixture
def tmp_settings(tmp_path: Path) -> Settings:
    """Settings rooted at a temp directory — no test pollutes another."""
    settings = Settings(data_dir=tmp_path / "memex_data", listen="127.0.0.1:0")
    settings.ensure_dirs()
    reset_settings_for_testing(settings)
    yield settings
    reset_settings_for_testing(None)


@pytest.fixture
def engine(tmp_settings: Settings) -> Engine:
    eng = Engine.build_default(tmp_settings)
    yield eng
    eng.close()
