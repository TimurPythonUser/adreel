"""Генерация сцен и монтаж: оценка, порядок, продолжение сцены, устойчивость к сбою.

Платных вызовов нет: картинки и клипы делают фейки, но ffmpeg настоящий —
именно он достаёт последний кадр клипа для продолжения.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import func, select
from test_music_analyze import click_track
from test_music_service import wav_bytes
from test_script import CARD, DIRECTION, good_script

from adreel.config import settings
from adreel.db.models import Clip, Project
from adreel.db.session import make_engine, make_sessions, upgrade
from adreel.gen import frames
from adreel.gen.fake import FakeImageGen, FakeVideoGen, solid_png
from adreel.gen.models import image_model, video_model
from adreel.gen.pipeline import Deps, GenerationError, GenerationService
from adreel.ingest.media import MediaStore
from adreel.llm.fake import FakeLLM
from adreel.music.service import MusicService
from adreel.product.service import Deps as ProductDeps
from adreel.product.service import ProductService
from adreel.render import ffmpeg, montage
from adreel.script.schema import Direction
from adreel.script.service import ScriptService
from adreel.tts.fake import FakeTTS

pytestmark = pytest.mark.ffmpeg

VMODEL = video_model("h3-max")
IMODEL = image_model()
KEEP = ("Preserve the exact steel mug shape, proportions, brushed finish, lid and label "
        "from the first frame.")
EN = ("Cinematic live-action. Steam rises from the mug on a wooden table. The camera pushes "
      "in with small amplitude at slow speed. Warm morning light.")


def grouped_plans() -> dict:
    """Сцены 1 и 2 (2.5 + 4 с) снимаются одной генерацией на 7 с."""
    def p(n, start, group=False, end="none"):
        return {"n": n, "preserve": KEEP, "description": EN, "avoid": "", "soundscape": "",
                "start_from": start, "chain": "frame", "end_with": end, "transition": "cut",
                "cut_note": "", "group_with_prev": group}

    return {"style_anchor": "cinematic 35mm, warm palette",
            "shots": [p(1, "product_photo"), p(2, "prev_last_frame", group=True),
                      p(3, "generated_scene"), p(4, "prev_last_frame"),
                      p(5, "product_photo", end="product_photo")]}


def plans_for(photo_id: int, *, seamless: bool = False) -> dict:
    """Сцена 1 — с фото, 2 — продолжает первую, 3 — новая сцена, 4 — продолжает третью,
    5 — финальная карточка с конечным кадром."""
    def p(n, start, end="none", chain="frame", group=False):
        return {"n": n, "preserve": KEEP, "description": EN, "avoid": "no melting handle",
                "soundscape": "Room tone.", "start_from": start, "chain": chain,
                "end_with": end, "transition": "cut", "cut_note": "резать на движении",
                "group_with_prev": group}

    return {"style_anchor": "cinematic 35mm, warm palette",
            "shots": [p(1, "product_photo"),
                      p(2, "prev_last_frame", chain="reference" if seamless else "frame"),
                      p(3, "generated_scene"), p(4, "prev_last_frame"),
                      p(5, "product_photo", "product_photo")]}


@dataclass
class World:
    products: ProductService
    scripts: ScriptService
    gen: GenerationService
    llm: FakeLLM
    imagegen: FakeImageGen
    videogen: FakeVideoGen
    sessions: object
    music: MusicService
    tts: FakeTTS

    def __iter__(self):
        return iter((self.products, self.scripts, self.gen, self.llm,
                     self.imagegen, self.videogen, self.sessions))


@pytest.fixture
async def world(tmp_path):
    url = f"sqlite+aiosqlite:///{(tmp_path / 'g.db').as_posix()}"
    upgrade(url)
    engine = make_engine(url)
    sessions = make_sessions(engine)
    media = MediaStore(tmp_path)
    products = ProductService(ProductDeps(sessions=sessions, media=media,
                                          text_llm=lambda: None, vision_llm=lambda: None,
                                          stt=lambda: None))
    llm = FakeLLM()
    scripts = ScriptService(sessions, products, lambda: llm, model=lambda: VMODEL,
                            chars_per_sec=lambda: 15.0, search=lambda: None)
    imagegen, videogen = FakeImageGen(360, 640), FakeVideoGen(360, 640)
    music = MusicService(sessions, media)
    tts = FakeTTS()
    gen = GenerationService(Deps(sessions=sessions, media=media, products=products,
                                 scripts=scripts, imagegen=lambda: imagegen,
                                 videogen=lambda: videogen, image_model=lambda: IMODEL,
                                 video_model=lambda: VMODEL, music=music, tts=lambda: tts,
                                 fonts_dir=settings.fonts_dir))
    yield World(products, scripts, gen, llm, imagegen, videogen, sessions, music, tts)
    await engine.dispose()


async def prepare(products, scripts, llm, *, shots_plan=None, seamless=False):
    pid = await products.new_draft(1, "uk")
    await products.save_card(pid, CARD)
    await products.add_photo(pid, solid_png(1000, 1000, (200, 60, 60)))
    photo_id = (await products.product_photos(pid))[0].id
    project_id = await scripts.start_project(pid)
    await scripts.choose_direction(project_id, Direction.model_validate(DIRECTION))
    await scripts.set_duration(project_id, 20)
    script = good_script()
    for sh in script["shots"]:
        sh["photo_ref"] = photo_id
    llm.responses = [script]
    await scripts.make_script(project_id)
    llm.responses = [shots_plan or plans_for(photo_id, seamless=seamless)]
    await scripts.make_shot_plans(project_id)
    return pid, project_id, photo_id


def test_palette_is_pulled_back_to_the_anchor():
    """Цепочка продолжений уползает в цвете — подтягиваем кадр к якорю ролика."""
    anchor = frames.to_vertical(solid_png(600, 1200, (120, 120, 120)), 108, 192)
    drifted = frames.to_vertical(solid_png(600, 1200, (150, 110, 90)), 108, 192)
    fixed = frames.match_palette(drifted, anchor)

    import io

    from PIL import Image, ImageStat
    def mean(data):
        with Image.open(io.BytesIO(data)) as im:
            return ImageStat.Stat(im.convert("RGB")).mean

    before, after, goal = mean(drifted), mean(fixed), mean(anchor)
    # синий канал был провален — после правки он ближе к якорю
    assert abs(after[2] - goal[2]) < abs(before[2] - goal[2])
    # правка ограничена: сцену не перекрашиваем целиком
    assert max(abs(a - b) for a, b in zip(after, before, strict=True)) < 60


def test_palette_untouched_when_colors_already_match():
    anchor = frames.to_vertical(solid_png(600, 1200, (120, 120, 120)), 108, 192)
    assert frames.match_palette(anchor, anchor) == anchor


def test_vertical_crop_fills_frame():
    tall = frames.to_vertical(solid_png(1000, 400), width=270, height=480)
    import io

    from PIL import Image
    with Image.open(io.BytesIO(tall)) as im:
        assert im.size == (270, 480)


async def test_estimate_counts_clips_and_scenes(world):
    products, scripts, gen, llm, *_ = world
    _, project_id, _ = await prepare(products, scripts, llm)
    est = await gen.estimate(project_id)
    # сцены 2.5/4/5/5/3.5 с -> клипы по 5 с (минимум модели) = 25 с
    assert est.clips == 5 and est.seconds == 25 and est.scenes == 1 and est.seamless == 0
    assert est.usd == pytest.approx(25 * VMODEL.price_per_sec + IMODEL.price_per_image, abs=0.01)
    assert "≈ $" in est.text


async def test_estimate_counts_seamless_continuation_separately(world):
    w = world
    _, project_id, _ = await prepare(w.products, w.scripts, w.llm, seamless=True)
    est = await w.gen.estimate(project_id)
    chain = video_model(VMODEL.chain_model)
    assert est.seamless == 1 and "бесшовных стыков 1" in est.text
    # одна сцена уходит на дорогую модель продолжения, остальные — на обычную
    expected = (20 * VMODEL.price_per_sec + chain.price(5, with_reference=True)
                + IMODEL.price_per_image)
    assert est.usd == pytest.approx(expected, abs=0.01)


async def test_generate_all_shots_in_order(world):
    products, scripts, gen, llm, imagegen, videogen, _ = world
    _, project_id, _ = await prepare(products, scripts, llm)
    steps: list[str] = []

    async def progress(text):
        steps.append(text)

    await gen.generate(project_id, progress=progress)

    assert len(imagegen.calls) == 1                     # картинка нужна только сцене 3
    assert imagegen.calls[0].startswith("Keep the product from the reference image")
    assert [d for _, d in videogen.calls] == [5, 5, 5, 5, 5]
    prompt = videogen.calls[0][0]
    assert "integrated_multimodal_description:" in prompt   # официальный формат H3
    assert prompt.index(KEEP) < prompt.index("Steam rises")  # контракт сохранения первым
    assert "cinematic 35mm" in prompt                        # общий стиль ушёл в промпт
    assert "non_diegetic_music: N/A" in prompt               # музыку у модели не просим
    assert any("продолжаю предыдущую сцену" in s for s in steps)

    rows = await gen.clips(project_id)
    assert [r.shot_n for r in rows] == [1, 2, 3, 4, 5]
    assert all(r.status == "ready" and r.clip_path and r.frame_path for r in rows)
    assert await gen.ready(project_id)


async def test_chained_shot_starts_from_previous_clip(world):
    products, scripts, gen, llm, *_ = world
    _, project_id, _ = await prepare(products, scripts, llm)
    await gen.generate(project_id)
    rows = {r.shot_n: r for r in await gen.clips(project_id)}
    # сцена 2 продолжает первую: его стартовая картинка — кадр из клипа 1, а не фото товара
    assert rows[2].frame_path != rows[1].frame_path
    assert rows[1].frame_path == rows[5].frame_path     # оба стартуют с одного фото товара


async def test_single_shot_regeneration(world):
    products, scripts, gen, llm, _, videogen, _ = world
    _, project_id, _ = await prepare(products, scripts, llm)
    await gen.generate(project_id)
    before = {r.shot_n: (r.version, r.clip_path) for r in await gen.clips(project_id)}
    videogen.calls.clear()

    await gen.generate(project_id, only=3)
    rows = {r.shot_n: r for r in await gen.clips(project_id)}
    assert len(videogen.calls) == 1                     # перегенерировали только сцену 3
    assert rows[3].version == before[3][0] + 1
    assert rows[1].version == before[1][0]

    with pytest.raises(GenerationError, match="сцены 9 нет"):
        await gen.generate(project_id, only=9)


async def test_failure_is_saved_and_does_not_lose_done_clips(world):
    products, scripts, gen, llm, _, videogen, _ = world
    _, project_id, _ = await prepare(products, scripts, llm)

    async def boom(image, prompt, duration, end_image=None, seed=None, progress=None):
        if len(videogen.calls) >= 2:
            raise RuntimeError("fal упал")
        videogen.calls.append((prompt, duration))
        return await FakeVideoGen(360, 640).image_to_video(image, prompt, duration)

    gen.d.videogen = lambda: type("V", (), {"image_to_video": staticmethod(boom),
                                            "can_continue": False})()
    with pytest.raises(GenerationError, match="сцена 3"):
        await gen.generate(project_id)

    rows = {r.shot_n: r for r in await gen.clips(project_id)}
    assert rows[1].status == "ready" and rows[2].status == "ready"
    assert rows[3].status == "failed" and "fal упал" in rows[3].error
    assert not await gen.ready(project_id)


async def test_resume_after_failure_pays_only_for_missing_scenes(world):
    """Главный денежный тест: «Доделать» не платит второй раз ни за клипы, ни за картинку."""
    w = world
    _, project_id, _ = await prepare(w.products, w.scripts, w.llm)
    real = w.gen.d.videogen
    calls = 0

    async def boom(image, prompt, duration, end_image=None, seed=None, progress=None):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise RuntimeError("fal упал")
        return await w.videogen.image_to_video(image, prompt, duration, seed=seed)

    w.gen.d.videogen = lambda: type("V", (), {"image_to_video": staticmethod(boom),
                                              "can_continue": False})()
    with pytest.raises(GenerationError, match="сцена 3"):
        await w.gen.generate(project_id)
    rows = {r.shot_n: r for r in await w.gen.clips(project_id)}
    # картинка сцены 3 оплачена и сохранена ДО вызова видеомодели
    assert len(w.imagegen.calls) == 1 and rows[3].frame_path
    frame_before = rows[3].frame_path

    est = await w.gen.estimate(project_id)
    assert est.clips == 3 and est.scenes == 0 and est.seconds == 15

    w.gen.d.videogen = real
    w.videogen.calls.clear()
    await w.gen.generate(project_id)
    assert [d for _, d in w.videogen.calls] == [5, 5, 5]     # только сцены 3–5
    assert len(w.imagegen.calls) == 1                        # картинку не перегенерили
    rows = {r.shot_n: r for r in await w.gen.clips(project_id)}
    assert rows[3].frame_path == frame_before                # и клип снят с неё же
    assert await w.gen.ready(project_id)
    assert "уже готовы" in (await w.gen.estimate(project_id)).text


async def test_video_cost_lands_in_cost_events(world):
    """Вложенный журнал подменял журнал товара — видео не попадало в учёт."""
    w = world
    pid, project_id, _ = await prepare(w.products, w.scripts, w.llm)
    w.videogen.usd_per_sec, w.imagegen.usd_per_image = 0.1, 0.05
    await w.gen.generate(project_id)
    assert await w.products.spent_usd(pid) == pytest.approx(25 * 0.1 + 0.05)
    rows = {r.shot_n: r for r in await w.gen.clips(project_id)}
    assert rows[1].usd == pytest.approx(0.5)                 # в клип — цена видео, без картинки
    assert rows[3].usd == pytest.approx(0.5)


async def test_parallel_generation_of_one_project_is_refused(world):
    """Двойной тап по «Утвердить» не должен запустить две платные генерации."""
    import asyncio

    w = world
    _, project_id, _ = await prepare(w.products, w.scripts, w.llm)
    first = asyncio.create_task(w.gen.generate(project_id))
    await asyncio.sleep(0)                                   # первая успела стартовать
    with pytest.raises(GenerationError, match="уже генерируется"):
        await w.gen.generate(project_id)
    with pytest.raises(GenerationError, match="ещё генерируется"):
        await w.gen.drop(project_id)
    await first
    assert len(w.videogen.calls) == 5


async def test_again_from_scratch_takes_a_new_seed(world):
    """С тем же сидом «заново» выдало бы почти то же, за что уже заплатили."""
    w = world
    _, project_id, _ = await prepare(w.products, w.scripts, w.llm)
    await w.gen.generate(project_id, only=1)
    async with w.sessions() as s:
        old = (await s.get(Project, project_id)).seed
    await w.gen.drop(project_id, reseed=True)
    await w.gen.generate(project_id, only=1)
    async with w.sessions() as s:
        assert (await s.get(Project, project_id)).seed not in (0, old)


async def test_drop_clears_clips(world):
    products, scripts, gen, llm, *_ = world
    _, project_id, _ = await prepare(products, scripts, llm)
    await gen.generate(project_id, only=1)
    assert await gen.clips(project_id)
    await gen.drop(project_id)
    assert await gen.clips(project_id) == []


async def test_generation_without_plan_is_refused(world):
    products, scripts, gen, *_ = world
    pid = await products.new_draft(1, "uk")
    await products.save_card(pid, CARD)
    project_id = await scripts.start_project(pid)
    with pytest.raises(GenerationError, match="нет сценария"):
        await gen.estimate(project_id)


async def test_clip_row_keeps_price_and_prompt(world):
    products, scripts, gen, llm, *_ = world
    _, project_id, _ = await prepare(products, scripts, llm)
    await gen.generate(project_id, only=1)
    row = (await gen.clips(project_id))[0]
    assert row.model == VMODEL.key and row.duration == 5
    assert "Steam rises" in row.prompt and "[Shot 1]" in row.prompt
    assert isinstance(row.usd, float)
    async with gen.d.sessions() as s:
        assert await s.scalar(select(func.count()).select_from(Clip)) == 1


# ---------- монтаж ----------

async def test_chained_scene_keeps_its_length(tmp_path):
    """Клип ровно в длину сцены минус срез на стыке — сегмент всё равно полной длины,
    иначе голос и субтитры уезжают на каждом продолжении."""
    clip = tmp_path / "c.mp4"
    clip.write_bytes(await ffmpeg.test_clip(5, 360, 640, 30))
    segs = await montage._segments([montage.ShotClip(1, clip, 5.0, chained=True)], tmp_path)
    assert (await ffmpeg.probe(segs[0])).duration == pytest.approx(5.0, abs=0.04)


def test_scene_text_becomes_caption_for_the_whole_scene():
    clips = [montage.ShotClip(1, Path("a"), 2.5, on_screen_text="Гаряча 12 годин"),
             montage.ShotClip(2, Path("b"), 4.0),
             montage.ShotClip(3, Path("c"), 3.0, on_screen_text="−20%")]
    assert [(c.start, c.end, c.text) for c in montage.scene_captions(clips)] == [
        (0.0, 2.5, "Гаряча 12 годин"), (6.5, 9.5, "−20%")]

async def with_voice_and_music(w: World) -> int:
    _, project_id, _ = await prepare(w.products, w.scripts, w.llm)
    w.llm.responses = [{"lines": [{"n": i, "text": "Кава гаряча щоранку."}
                                  for i in range(1, 6)], "claims": []}]
    await w.scripts.make_voiceover(project_id)
    pending = await w.music.prepare_upload(1, wav_bytes(click_track(60, drop_at=24)), "beat.wav")
    track = await w.music.save_track(1, pending)
    await w.music.choose(project_id, track.id)
    return project_id


async def test_assemble_returns_one_ready_video(world, tmp_path):
    w = world
    project_id = await with_voice_and_music(w)
    await w.gen.generate(project_id)
    video = await w.gen.assemble(project_id)

    out = tmp_path / "final.mp4"
    out.write_bytes(video)
    info = await ffmpeg.probe(out)
    assert (info.width, info.height) == (1080, 1920)     # вертикаль под TikTok и Reels
    assert info.has_audio and round(info.fps) == 30
    assert info.duration == pytest.approx(20, abs=0.7)   # длина сценария, а не сумма клипов
    assert len(w.tts.calls) == 5                         # строка озвучки на каждую сцену

    async with w.sessions() as s:
        assert (await s.get(Project, project_id)).video_path


async def test_assemble_without_voice_and_music_still_makes_video(world, tmp_path):
    w = world
    _, project_id, _ = await prepare(w.products, w.scripts, w.llm)
    await w.gen.generate(project_id)
    out = tmp_path / "silent.mp4"
    out.write_bytes(await w.gen.assemble(project_id))
    info = await ffmpeg.probe(out)
    assert (info.width, info.height) == (1080, 1920) and not info.has_audio
    assert not w.tts.calls


async def test_assemble_refuses_when_a_scene_is_missing(world):
    w = world
    _, project_id, _ = await prepare(w.products, w.scripts, w.llm)
    await w.gen.generate(project_id, only=1)
    with pytest.raises(GenerationError, match="нет клипов для сцен"):
        await w.gen.assemble(project_id)


# ---------- сид и бесшовное продолжение ----------

async def test_seed_is_stable_for_the_project(world):
    w = world
    _, project_id, _ = await prepare(w.products, w.scripts, w.llm)
    await w.gen.generate(project_id)
    rows = {r.shot_n: r for r in await w.gen.clips(project_id)}
    async with w.sessions() as s:
        seed = (await s.get(Project, project_id)).seed
    assert seed and rows[1].seed == seed + 1 and rows[5].seed == seed + 5
    assert w.videogen.seeds == [seed + n for n in range(1, 6)]

    # повтор сцены платит тем же сидом: результат воспроизводим
    w.videogen.seeds.clear()
    await w.gen.generate(project_id, only=3)
    assert w.videogen.seeds == [seed + 3]


async def test_seamless_continuation_sends_clip_tail(world):
    w = world
    _, project_id, _ = await prepare(w.products, w.scripts, w.llm, seamless=True)
    await w.gen.generate(project_id)

    assert len(w.videogen.continues) == 1                 # только помеченный стык
    prompt, duration, tail_bytes = w.videogen.continues[0]
    assert duration == 5 and tail_bytes > 1000            # ушёл настоящий кусок видео
    assert "seamless continuation of <Video 1>" in prompt
    rows = {r.shot_n: r for r in await w.gen.clips(project_id)}
    chain = video_model(VMODEL.chain_model)
    assert rows[2].model == chain.key                     # и записали, чем сняли
    assert rows[2].status == "ready" and rows[2].frame_path


async def test_chain_without_continuation_support_falls_back_to_frame(world):
    w = world
    _, project_id, _ = await prepare(w.products, w.scripts, w.llm, seamless=True)
    w.videogen.can_continue = False
    await w.gen.generate(project_id)
    assert not w.videogen.continues
    rows = {r.shot_n: r for r in await w.gen.clips(project_id)}
    assert rows[2].status == "ready" and rows[2].model == VMODEL.key


# ---------- группы сцен ----------

async def test_group_is_one_generation_split_into_scenes(world):
    w = world
    _, project_id, _ = await prepare(w.products, w.scripts, w.llm, shots_plan=grouped_plans())
    await w.gen.generate(project_id)

    # 5 сцен, но генераций 4: первые две сняты одним куском на 2.5 + 4 = 7 с
    assert len(w.videogen.calls) == 4
    assert [d for _, d in w.videogen.calls] == [7, 5, 5, 5]
    prompt = w.videogen.calls[0][0]
    assert "[Shot 2] At 00:02.500, the camera cuts to:" in prompt

    rows = {r.shot_n: r for r in await w.gen.clips(project_id)}
    assert len(rows) == 5 and all(r.status == "ready" for r in rows.values())
    # обе сцены группы получили свой кусок клипа и половину счёта
    assert rows[1].clip_path != rows[2].clip_path
    assert rows[1].seed == rows[2].seed
    first = await ffmpeg.probe(w.gen.d.media.path(rows[1].clip_path))
    second = await ffmpeg.probe(w.gen.d.media.path(rows[2].clip_path))
    assert first.duration == pytest.approx(2.5, abs=0.2)
    assert second.duration == pytest.approx(4.0, abs=0.2)


async def test_group_estimate_is_cheaper_than_separate_scenes(world):
    w = world
    _, project_id, _ = await prepare(w.products, w.scripts, w.llm, shots_plan=grouped_plans())
    grouped = await w.gen.estimate(project_id)

    _, plain_id, _ = await prepare(w.products, w.scripts, w.llm)
    plain = await w.gen.estimate(plain_id)

    assert grouped.generations == 4 and plain.generations == 5
    assert grouped.seconds == 22 and plain.seconds == 25     # минимум 5 с платим реже
    assert grouped.usd < plain.usd
    assert "5 сцен за 4 генераций" in grouped.text


async def test_regenerating_a_grouped_scene_redoes_the_whole_generation(world):
    w = world
    _, project_id, _ = await prepare(w.products, w.scripts, w.llm, shots_plan=grouped_plans())
    await w.gen.generate(project_id)
    w.videogen.calls.clear()

    await w.gen.generate(project_id, only=2)      # сцену 2 в одиночку не переснять
    assert [d for _, d in w.videogen.calls] == [7]
    rows = {r.shot_n: r for r in await w.gen.clips(project_id)}
    assert rows[1].version == 2 and rows[2].version == 2


async def test_grouped_video_assembles_to_full_length(world, tmp_path):
    w = world
    _, project_id, _ = await prepare(w.products, w.scripts, w.llm, shots_plan=grouped_plans())
    await w.gen.generate(project_id)
    out = tmp_path / "grouped.mp4"
    out.write_bytes(await w.gen.assemble(project_id))
    info = await ffmpeg.probe(out)
    assert (info.width, info.height) == (1080, 1920)
    assert info.duration == pytest.approx(20, abs=0.7)
