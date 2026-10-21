"""Живые проверки платных API. Только вручную: RUN_LIVE_TESTS=1 uv run pytest -m live.

Цель — один раз подтвердить схемы моделей с verified: false в config/video_models.yaml.
"""

from __future__ import annotations

import pytest

from adreel import costs
from adreel.config import settings
from adreel.gen import frames, make_image_gen, make_video_gen
from adreel.gen.base import ImageInput
from adreel.gen.fake import solid_png
from adreel.gen.models import video_model
from adreel.render import ffmpeg
from adreel.script.shots import ShotPlan, ShotPlans
from adreel.tts.elevenlabs import make_tts

pytestmark = pytest.mark.live


async def test_fal_image_edit_roundtrip():
    """Самый дешёвый живой вызов fal (~$0.04): ключ живой, схема картинок верна."""
    if not settings.fal_key:
        pytest.skip("нет FAL_KEY")
    ledger = costs.Ledger("live")
    with costs.job_scope(ledger):
        data = await make_image_gen().edit(
            [ImageInput(solid_png(512, 512, (200, 60, 60)))],
            "a red square product box standing on a wooden table, soft daylight",
            aspect_ratio="9:16",
        )
    assert data[:4] in (b"\x89PNG", b"\xff\xd8\xff\xe0", b"\xff\xd8\xff\xe1", b"RIFF")
    assert ledger.total_usd > 0


async def test_elevenlabs_timings_cover_text():
    if not settings.elevenlabs_api_key:
        pytest.skip("нет ELEVENLABS_API_KEY")
    speech = await make_tts().synthesize("Кава холоне, поки ти доїдеш до роботи? Є рішення.")
    assert speech.duration > 1
    assert speech.words[0].text.startswith("Кава")


#: для живых проверок берём самый дешёвый режим: проверяем СХЕМУ, а не качество
TEST_MODEL = "h3-max-turbo-480"


def _plan(description: str) -> ShotPlans:
    return ShotPlans(style_anchor="cinematic 35mm, warm daylight", shots=[ShotPlan(
        n=1,
        preserve="Preserve the exact red box shape, proportions, color and surface "
                 "from the first frame.",
        description=description,
        avoid="no melting edges",
        soundscape="Room tone.",
        start_from="product_photo")])


async def test_fal_h3_image_to_video_schema():
    """Схема H3 image-to-video: 5 с в 480P ≈ $0.13. Промпт в официальном формате."""
    if not settings.fal_key:
        pytest.skip("нет FAL_KEY")
    model = video_model(TEST_MODEL)
    plans = _plan("Cinematic live-action. The red box rests on a wooden table while dust "
                  "motes drift through the light. The camera pushes in with small amplitude "
                  "at slow speed. Warm daylight from the left.")
    ledger = costs.Ledger("live")
    with costs.job_scope(ledger):
        clip = await make_video_gen(TEST_MODEL).image_to_video(
            ImageInput(solid_png(1080, 1920, (200, 60, 60))),
            plans.prompt_for(1, duration=5), 5, seed=42)
    probe = await ffmpeg.probe_bytes(clip)
    assert probe.duration >= 4.5 and probe.width and probe.height
    assert ledger.total_usd == pytest.approx(5 * model.price_per_sec, abs=0.01)


async def test_fal_h3_seamless_continuation_schema():
    """Схема продолжения по хвосту клипа (reference-to-video). Дороже: ~$0.55."""
    if not settings.fal_key:
        pytest.skip("нет FAL_KEY")
    gen = make_video_gen(TEST_MODEL)
    if not gen.can_continue:
        pytest.skip("у модели нет chain_model")
    first = await gen.image_to_video(
        ImageInput(solid_png(1080, 1920, (200, 60, 60))),
        _plan("Cinematic live-action. The red box on a table, the camera trucks left with "
              "small amplitude at slow speed.").prompt_for(1, duration=5), 5, seed=42)
    tail = await frames.tail_clip(first, 1.0)
    clip = await gen.continue_video(
        tail, [ImageInput(solid_png(1080, 1920, (200, 60, 60)))],
        _plan("Cinematic live-action. The camera keeps trucking left and reveals a window "
              "behind the box.").prompt_for(1, duration=5, reference=True), 5, seed=42)
    assert (await ffmpeg.probe_bytes(clip)).duration >= 4.5
