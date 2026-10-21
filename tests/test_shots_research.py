"""Оператор (промпты и непрерывность) и рыночная выжимка из интернета."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from test_script import CARD, DIRECTION, good_script

from adreel.db.models import Product
from adreel.db.session import make_engine, make_sessions, upgrade
from adreel.gen.models import video_model
from adreel.ingest.media import MediaStore
from adreel.llm.fake import FakeLLM
from adreel.product.service import Deps, ProductService
from adreel.research.fake import FakeSearch
from adreel.research.notes import ResearchNotes, collect, queries
from adreel.research.search import SearchHit
from adreel.script import shots
from adreel.script.creative import Brief, Direction, ScriptError
from adreel.script.schema import VideoScript
from adreel.script.service import ScriptService
from adreel.script.shots import ShotPlans

MODEL = video_model("h3-max")
SCRIPT = VideoScript.model_validate(good_script())
KEEP = ("Preserve the exact steel mug shape, proportions, brushed finish, lid and label "
        "from the first frame.")
EN = ("Cinematic live-action. Steam rises from the mug on a wooden table and drifts left. "
      "The camera pushes in with small amplitude at slow speed. Warm morning light.")


def plan(n, start="generated_scene", end="none", transition="cut", prompt=EN, chain="frame",
         preserve=KEEP, sound="Room tone and a soft kettle hiss.", group=False):
    return {"n": n, "preserve": preserve, "description": prompt, "avoid": "no melting handle",
            "soundscape": sound, "start_from": start, "chain": chain, "end_with": end,
            "transition": transition, "cut_note": "резать на движении",
            "group_with_prev": group}


def plans(*items, anchor="cinematic 35mm, warm palette, soft daylight"):
    return {"style_anchor": anchor, "shots": list(items)}


def good_plans():
    return plans(plan(1, "product_photo"), plan(2, "prev_last_frame"), plan(3),
                 plan(4, "prev_last_frame"), plan(5, "product_photo", end="product_photo"))


def check(raw):
    return shots.check_plans(ShotPlans.model_validate(raw), SCRIPT, MODEL)


def test_good_plan_passes():
    assert check(good_plans()) == []


def test_first_shot_cannot_continue_previous():
    bad = plans(plan(1, "prev_last_frame"), plan(2), plan(3), plan(4), plan(5))
    assert any("сцена 1 не может продолжать" in e for e in check(bad))


def test_long_chain_is_rejected():
    bad = plans(plan(1, "product_photo"), *[plan(n, "prev_last_frame") for n in range(2, 6)])
    assert any("больше 3 продолжений" in e for e in check(bad))


def test_prompt_must_be_english_and_about_one_shot():
    bad = plans(plan(1, "product_photo", prompt="Крупный план кружки, камера медленно "
                     "наезжает, pushes in", preserve="Сохрани форму и цвет кружки"),
                plan(2, prompt=EN + " Then cut to the kitchen."), plan(3), plan(4), plan(5))
    text = " | ".join(check(bad))
    assert "на английском" in text and "«cut to»" in text


def test_camera_and_preservation_are_required():
    bad = plans(plan(1, "product_photo", preserve=""),
                plan(2, prompt="Cinematic live-action. The mug sits on the table."),
                plan(3), plan(4), plan(5))
    text = " | ".join(check(bad))
    assert "preserve пустой" in text
    assert "движение камеры словарём модели" in text


def test_talking_heads_and_screen_text_are_rejected():
    bad = plans(plan(1, "product_photo",
                     prompt=EN + " A woman says hello to the camera."),
                plan(2, prompt=EN + " A caption appears over the shot."),
                plan(3), plan(4), plan(5))
    text = " | ".join(check(bad))
    assert "lips remain completely closed" in text
    assert "оверлеем" in text


def test_music_is_never_ordered_from_the_model():
    bad = plans(plan(1, "product_photo", sound="Upbeat music and room tone."),
                plan(2), plan(3), plan(4), plan(5))
    assert any("музыку у модели не заказываем" in e for e in check(bad))


def test_whip_pan_must_be_written_on_both_sides():
    bad = plans(plan(1, "product_photo", transition="whip_pan"), plan(2), plan(3), plan(4),
                plan(5))
    text = " | ".join(check(bad))
    assert "нужен быстрый смаз" in text and "выходить из смаза" in text

    ok = plans(plan(1, "product_photo", transition="whip_pan",
                    prompt=EN + " The camera whips quickly to the right, motion blur."),
               plan(2, prompt="The camera decelerates from a fast rightward pan, motion blur "
                              "settling. Static hold on the mug. " + EN),
               plan(3), plan(4), plan(5))
    assert check(ok) == []


def test_seamless_continuation_is_limited():
    bad = plans(plan(1, "product_photo"), plan(2, "prev_last_frame", chain="reference"),
                plan(3), plan(4, "prev_last_frame", chain="reference"),
                plan(5, "product_photo", chain="reference"))
    text = " | ".join(check(bad))
    assert "chain = reference только для продолжения" in text

    many = plans(plan(1, "product_photo"), plan(2, "prev_last_frame", chain="reference"),
                 plan(3, "prev_last_frame", chain="reference"),
                 plan(4, "prev_last_frame", chain="reference"), plan(5, "product_photo"))
    assert any("не больше 2" in e for e in check(many))


def test_end_frame_rules():
    bad = plans(plan(1, "product_photo"), plan(2), plan(3),
                plan(4, transition="morph"), plan(5, end="next_start", transition="whip_pan"))
    text = " | ".join(check(bad))
    assert "morph работает только" in text
    assert "end_with не может быть next_start" in text
    assert "у последней сцены transition = cut" in text


def test_prompt_is_built_in_official_h3_format():
    p = ShotPlans.model_validate(good_plans())
    text = p.prompt_for(1)
    assert text.startswith("For the target video, at 0.00 seconds")
    assert "integrated_multimodal_description:" in text and "[Shot 1]" in text
    assert text.index(KEEP) < text.index("Steam rises")      # контракт сохранения первым
    assert "cinematic 35mm, warm palette, soft daylight" in text
    assert "no soft dissolves or morphs" in text             # общие негативы добавил код
    assert text.rstrip().endswith("non_diegetic_music: N/A")
    assert p.chained == [2, 4]

    with_end = p.prompt_for(5, duration=5, end_image=True)
    assert "<Picture 2> aligns with the video end at 5.00 seconds" in with_end

    cont = p.prompt_for(2, duration=5, reference=True)
    assert "seamless continuation of <Video 1>" in cont
    assert "First frame of [Shot 1] is the last frame of <Video 1>" in cont


async def test_agent_fixes_plan_after_errors():
    broken = plans(plan(1, "prev_last_frame"), plan(2), plan(3), plan(4), plan(5))
    llm = FakeLLM([broken, good_plans()])
    brief = Brief(CARD, [], [])
    res = await shots.plan_shots(llm, brief, Direction.model_validate(DIRECTION), SCRIPT, MODEL)
    assert res.shots[0].start_from == "product_photo"
    retry = llm.calls[1][1]
    assert "сцена 1 не может продолжать" in retry
    # правила модели из config/video_models.yaml дошли до агента
    assert "integrated_multimodal_description" in llm.calls[0][0]
    assert "бесшовное продолжение" in llm.calls[0][0]


async def test_agent_gives_up_with_reason():
    broken = plans(plan(1, "prev_last_frame"), plan(2), plan(3), plan(4), plan(5))
    llm = FakeLLM([broken, broken, broken])
    with pytest.raises(ScriptError, match="сцена 1"):
        await shots.plan_shots(llm, Brief(CARD, [], []), Direction.model_validate(DIRECTION),
                               SCRIPT, MODEL)


# ---------- группы ----------

def test_group_splits_generations():
    p = ShotPlans.model_validate(plans(
        plan(1, "product_photo"), plan(2, "prev_last_frame", group=True),
        plan(3), plan(4, "prev_last_frame"), plan(5, "product_photo")))
    assert [[x.n for x in g] for g in p.groups()] == [[1, 2], [3], [4], [5]]
    assert [x.n for x in p.group_of(2)] == [1, 2]


def test_group_prompt_has_timecoded_shot_blocks():
    p = ShotPlans.model_validate(plans(plan(1, "product_photo"),
                                       plan(2, "prev_last_frame", group=True),
                                       plan(3), plan(4), plan(5)))
    text = p.group_prompt(p.group_of(1), [0.0, 2.5], duration=7)
    assert "[Shot 1] " in text
    assert "[Shot 2] At 00:02.500, the camera cuts to:" in text
    assert text.index("[Shot 1]") < text.index("[Shot 2]")
    assert text.count("overall_soundscape:") == 1          # три поля на всю генерацию
    assert text.rstrip().endswith("non_diegetic_music: N/A")


def test_group_members_have_no_own_start_or_end():
    bad = plans(plan(1, "product_photo"),
                plan(2, "generated_scene", group=True, end="next_start"),
                plan(3, "prev_last_frame", group=True, chain="reference"),
                plan(4), plan(5))
    text = " | ".join(check(bad))
    assert "внутри группы нет своего стартового кадра" in text
    assert "end_with работает только у последней сцены группы" in text
    assert "внутри группы chain всегда" in text


def test_group_longer_than_model_max_is_rejected():
    long_script = VideoScript.model_validate(good_script(30))
    grouped = plans(plan(1, "product_photo"), plan(2, "prev_last_frame", group=True),
                    plan(3, "prev_last_frame", group=True),
                    plan(4, "prev_last_frame", group=True),
                    plan(5, "prev_last_frame", group=True))
    errors = shots.check_plans(ShotPlans.model_validate(grouped), long_script, MODEL)
    assert any("максимум 15 с" in e for e in errors)


def test_first_scene_cannot_join_a_group():
    bad = plans(plan(1, "product_photo", group=True), plan(2), plan(3), plan(4), plan(5))
    assert any("не может продолжать группу" in e for e in check(bad))


def test_grouped_scenes_do_not_count_as_a_chain_run():
    ok = plans(plan(1, "product_photo"),
               *[plan(n, "prev_last_frame", group=True) for n in (2, 3, 4)],
               plan(5, "product_photo"))
    # четыре сцены подряд «продолжают предыдущую», но это одна генерация — не цепочка
    assert not any("больше 3 продолжений" in e for e in check(ok))


# ---------- рынок ----------

def test_queries_cover_pains_and_format():
    qs = " | ".join(queries(CARD))
    assert "отзывы" in qs and "tiktok" in qs


async def test_collect_summarizes_hits():
    search = FakeSearch([SearchHit("Отзыв", "https://a.test", "кофе остывает за час")])
    llm = FakeLLM([{"pains": ["кофе остывает"], "hooks": ["показать пар"],
                    "phrases": ["тримає тепло"], "sources": ["https://a.test"]}])
    notes = await collect(search, llm, CARD)
    assert notes.pains == ["кофе остывает"] and not notes.empty
    assert "Боли покупателей" in notes.block()
    assert len(search.queries) == 3
    assert "https://a.test" in llm.calls[0][1]


async def test_no_hits_means_no_notes():
    notes = await collect(FakeSearch([]), FakeLLM([]), CARD)
    assert notes.empty and notes.block() == ""


def test_notes_expire_after_a_month():
    fresh = ResearchNotes(pains=["x"], fetched_at=datetime.now(UTC).isoformat())
    old = ResearchNotes(pains=["x"],
                        fetched_at=(datetime.now(UTC) - timedelta(days=40)).isoformat())
    assert fresh.fresh() and not old.fresh()
    assert not ResearchNotes(pains=["x"]).fresh()      # без даты — считаем протухшей


@pytest.fixture
async def world(tmp_path):
    url = f"sqlite+aiosqlite:///{(tmp_path / 'r.db').as_posix()}"
    upgrade(url)
    engine = make_engine(url)
    sessions = make_sessions(engine)
    products = ProductService(Deps(sessions=sessions, media=MediaStore(tmp_path),
                                   text_llm=lambda: None, vision_llm=lambda: None,
                                   stt=lambda: None))
    llm = FakeLLM()
    search = FakeSearch([SearchHit("Отзыв", "https://a.test", "кофе остывает")])
    scripts = ScriptService(sessions, products, lambda: llm, model=lambda: MODEL,
                            chars_per_sec=lambda: 15.0, search=lambda: search)
    yield products, scripts, llm, search, sessions
    await engine.dispose()


async def test_research_is_cached_and_lands_in_brief(world):
    products, scripts, llm, search, sessions = world
    pid = await products.new_draft(1, "uk")
    await products.save_card(pid, CARD)

    llm.responses = [{"pains": ["кофе остывает"], "hooks": ["пар в кадре"]}]
    brief = await scripts.brief(pid)
    assert "Боли покупателей: кофе остывает" in brief.market
    async with sessions() as s:
        assert (await s.get(Product, pid)).research is not None

    llm.responses = []                      # второй раз поиска быть не должно — есть свежая
    again = await scripts.brief(pid)
    assert again.market == brief.market
    assert len(search.queries) == 3


async def test_without_search_key_everything_still_works(world):
    products, _, llm, _, sessions = world
    pid = await products.new_draft(1, "uk")
    await products.save_card(pid, CARD)
    scripts = ScriptService(sessions, products, lambda: llm, model=lambda: MODEL,
                            chars_per_sec=lambda: 15.0, search=lambda: None)
    brief = await scripts.brief(pid)
    assert brief.market == ""


async def test_shot_plans_saved_and_reset_with_new_script(world):
    products, scripts, llm, _, _ = world
    pid = await products.new_draft(1, "uk")
    await products.save_card(pid, CARD)
    project_id = await scripts.start_project(pid)
    await scripts.choose_direction(project_id, Direction.model_validate(DIRECTION))
    await scripts.set_duration(project_id, 20)

    # первый ответ — рыночная выжимка (её закешируют), второй — сценарий
    llm.responses = [{"pains": ["кофе остывает"]}, good_script()]
    await scripts.make_script(project_id)
    llm.responses = [good_plans()]
    saved = await scripts.make_shot_plans(project_id)
    assert saved.chained == [2, 4]
    assert (await scripts.get(project_id)).shot_plan is not None

    llm.responses = [good_script()]
    await scripts.make_script(project_id, "variant")
    assert (await scripts.get(project_id)).shot_plan is None   # план под старые кадры устарел
