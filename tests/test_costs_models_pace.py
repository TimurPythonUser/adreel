from __future__ import annotations

import pytest

from adreel import costs, pace
from adreel.config import settings
from adreel.gen.models import all_video_models, image_model, pick, video_model


def test_llm_price_from_config():
    assert costs.llm_usd("claude-sonnet-5", 1_000_000, 1_000_000) == pytest.approx(12.0)
    with pytest.raises(KeyError, match=r"pricing\.yaml"):
        costs.llm_usd("unknown-model", 1, 1)


def test_ledger_groups_and_totals():
    ledger = costs.Ledger("p1")
    with costs.job_scope(ledger):
        costs.record("fal", "kling", 5, "video_sec", 0.7)
        costs.record("fal", "kling", 5, "video_sec", 0.7)
        costs.record("elevenlabs", "v3", 300, "chars", 0.05)
    costs.record("fal", "kling", 5, "video_sec", 99)   # вне scope — не пишется
    assert ledger.total_usd == pytest.approx(1.45)
    lines = {name: (units, usd) for name, units, _, usd in ledger.lines()}
    assert lines["fal / kling"] == (10, pytest.approx(1.4))
    assert "Итого: $1.450" in costs.format_report(ledger)


def test_unknown_model_price_does_not_crash_paid_call():
    ledger = costs.Ledger("p")
    with costs.job_scope(ledger):
        costs.record_llm("anthropic", "unknown-model", 10, 10)
    assert ledger.total_usd == 0


def test_video_models_catalog_is_valid():
    models = all_video_models()
    assert models
    for m in models:
        assert m.durations == sorted(m.durations)
        assert m.max_sec <= 15
    assert video_model().key
    assert image_model().fal_id


def test_clip_duration_never_shorter_than_needed():
    m = video_model("h3-max-turbo")     # durations 5..15
    assert m.clip_duration(2.3) == 5
    assert m.clip_duration(5.0) == 5
    assert m.clip_duration(5.1) == 6
    with pytest.raises(ValueError, match="длиннее максимума"):
        m.clip_duration(16)


def test_arguments_map_to_model_fields():
    k = video_model("kling-o3-pro")
    args = k.arguments(image_url="u1", prompt="p", duration=5, end_image_url="u2")
    assert args["image_url"] == "u1"
    assert args["end_image_url"] == "u2"
    assert args["duration"] == "5"               # Kling хочет строку
    assert args["generate_audio"] is False
    with pytest.raises(ValueError):
        k.arguments(image_url="u", prompt="p", duration=99)


def test_pick_path():
    assert pick({"images": [{"url": "x"}]}, "images.0.url") == "x"


def test_pace_uses_estimate_until_enough_samples(monkeypatch):
    monkeypatch.setattr(settings, "tts_tempo", 1.0)
    assert pace.chars_per_sec() == pace.DEFAULT_CHARS_PER_SEC
    for _ in range(pace.MIN_SAMPLES):
        pace.observe(200, 10.0)
    assert pace.chars_per_sec() == pytest.approx(20.0)
    assert "замер" in pace.describe()


def test_pace_resets_when_voice_changes(monkeypatch):
    for _ in range(pace.MIN_SAMPLES):
        pace.observe(200, 10.0)
    monkeypatch.setattr(settings, "elevenlabs_voice_id", "другой-голос")
    assert "оценка" in pace.describe()
