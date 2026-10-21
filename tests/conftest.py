from __future__ import annotations

import os

import pytest

from adreel.config import settings
from adreel.render import ffmpeg


@pytest.fixture(autouse=True)
def _isolated_workdir(tmp_path, monkeypatch):
    """Каждый тест пишет свои файлы (pace.json, pending) во временную папку.

    Заодно гасим ключ поиска: с ключом из .env тесты полезли бы в интернет за деньги.
    Нужен поиск в тесте — передай свой SearchProvider (tests/test_shots_research.py).
    """
    monkeypatch.setattr(settings, "workdir", tmp_path / "workdir")
    monkeypatch.setattr(settings, "tavily_api_key", "")


def pytest_collection_modifyitems(config, items):
    live = os.environ.get("RUN_LIVE_TESTS") == "1"
    for item in items:
        if "live" in item.keywords and not live:
            item.add_marker(pytest.mark.skip(reason="платный API: нужен RUN_LIVE_TESTS=1"))
        if "ffmpeg" in item.keywords and not ffmpeg.available():
            item.add_marker(pytest.mark.skip(reason="нет ffmpeg в PATH"))
