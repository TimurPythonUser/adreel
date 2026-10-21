"""Подделки, которые реально гоняют ffmpeg: проверяем, что на них можно строить монтаж."""

from __future__ import annotations

import pytest

from adreel.gen.base import ImageInput
from adreel.gen.fake import FakeImageGen, FakeVideoGen, solid_png
from adreel.render import ffmpeg
from adreel.tts.fake import FakeTTS

pytestmark = pytest.mark.ffmpeg


async def test_fake_video_is_real_mp4(tmp_path):
    gen = FakeVideoGen(width=360, height=640)
    data = await gen.image_to_video(ImageInput(solid_png(8, 8)), "zoom in", 2)
    path = tmp_path / "clip.mp4"
    path.write_bytes(data)
    p = await ffmpeg.probe(path)
    assert (p.width, p.height) == (360, 640)
    assert p.duration == pytest.approx(2.0, abs=0.1)
    assert not p.has_audio


async def test_fake_tts_audio_matches_duration(tmp_path):
    speech = await FakeTTS(chars_per_sec=10).synthesize("Кава холоне поки ти їдеш")
    assert speech.words[-1].end <= speech.duration
    path = tmp_path / "vo.mp3"
    path.write_bytes(speech.audio)
    out = await ffmpeg.run(["-i", str(path), "-f", "null", "-"])
    assert out == b""


async def test_fake_image_is_png():
    data = await FakeImageGen(width=4, height=4).edit([], "scene")
    assert data.startswith(b"\x89PNG")
