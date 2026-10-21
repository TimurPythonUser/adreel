"""Разбор трека на синтетике с известными параметрами: темп, биты, дроп."""

from __future__ import annotations

import numpy as np
import pytest

from adreel.music.analyze import SR, MusicError, analyze

BPM = 120
BEAT = 60 / BPM


def click_track(seconds: float, drop_at: float) -> np.ndarray:
    """Клики каждые 0.5 с (120 BPM), каждый 4-й громче; после drop_at всё громче и гуще."""
    y = np.zeros(int(seconds * SR), dtype=np.float32)
    n = int(0.03 * SR)
    t = np.arange(n) / SR
    click = (np.sin(2 * np.pi * 1000 * t) * np.exp(-t * 80)).astype(np.float32)
    rng = np.random.default_rng(0)
    for i, start in enumerate(np.arange(0, seconds - 0.05, BEAT)):
        a = int(start * SR)
        gain = 1.0 if i % 4 == 0 else 0.5
        if start < drop_at:
            gain *= 0.25
        y[a:a + n] += gain * click
    after = int(drop_at * SR)
    y[after:] += (0.15 * rng.standard_normal(len(y) - after)).astype(np.float32)
    return y


@pytest.fixture(scope="module")
def mmap():
    return analyze(click_track(60, drop_at=24))


def test_bpm_detected(mmap):
    assert mmap.bpm == pytest.approx(BPM, abs=2)


def test_beats_on_grid(mmap):
    grid = np.arange(0, 60, BEAT)
    errors = [np.min(np.abs(grid - b)) for b in mmap.beats]
    assert np.median(errors) < 0.05


def test_downbeats_every_four_beats(mmap):
    gaps = np.diff(mmap.downbeats)
    assert np.median(gaps) == pytest.approx(4 * BEAT, abs=0.05)


def test_drop_found_and_used_in_window(mmap):
    assert any(abs(d - 24) <= 1.0 for d in mmap.drops)
    w = mmap.best(20)
    assert w.end - w.start == pytest.approx(20)
    assert w.drop_at is not None and 2 <= w.drop_at <= 12
    assert w.start in mmap.downbeats                   # кусок начинается в такт


def test_alternative_windows_do_not_overlap_much(mmap):
    starts = [w.start for w in mmap.windows["15"]]
    assert len(starts) >= 2
    assert all(abs(a - b) >= 7.5 for i, a in enumerate(starts) for b in starts[i + 1:])


def test_short_track_loops_and_limits():
    short = analyze(click_track(12, drop_at=4))
    assert short.best(30).loop and short.best(30).end == pytest.approx(12, abs=0.1)
    with pytest.raises(MusicError, match="короче"):
        analyze(np.zeros(SR * 3, dtype=np.float32))
    assert "BPM" in short.describe(15)
