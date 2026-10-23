"""Креатив: правила сценария и озвучки, переделка по ошибкам, путь целиком на фейковой модели."""

from __future__ import annotations

import pytest
from test_music_analyze import click_track

from adreel.db.models import Project, Track
from adreel.db.session import make_engine, make_sessions, upgrade
from adreel.gen.fake import solid_png
from adreel.ingest.media import MediaStore
from adreel.llm.fake import FakeLLM
from adreel.music.analyze import analyze
from adreel.music.service import MusicService
from adreel.product.schema import Fact, ProductCard
from adreel.product.service import Deps, ProductService
from adreel.prompts import load
from adreel.script import creative, validate
from adreel.script.schema import VideoScript, Voiceover
from adreel.script.service import ScriptService

CARD = ProductCard(
    language="uk", name="Термокружка Steel", category="home", cta="Замовляй на сайті",
    benefits=[Fact(text="тримає тепло 8 годин", source="page:1")],
    forbidden_claims=["найкраща в Україні"], answered=["offer"],
)

DIRECTION = {
    "title": "Кава не холоне", "format": "проблема → рішення", "hook": "холодна кава",
    "idea": "ранок, пробка, кава холоне",
    "beats": ["Олена в пробці відпиває каву і кривиться", "дощ, машини стоять",
              "термокружка: з-під кришки вилітає пара", "різке гальмування — ні краплі"],
    "cast": "Олена, 30 років, руде каре, гірчичний светр", "world": "салон авто, пробка",
    "visual_style": "теплий світ", "voice_tone": "дружній",
    "music_mood": "легкий поп", "why_it_works": "біль знайома", "risks": "",
}
OLENA = {"name": "Олена", "look": "жінка 30 років, руде каре, гірчичний светр"}


def shot(n, dur, role, ref=11, text="", *, size="medium", where="кухня", event="",
         source="generated_scene", cast=()):
    return {"n": n, "duration_sec": dur, "role": role, "visual": f"кадр {n}",
            "event": event or f"подія {n}: щось змінюється в кадрі", "camera": "наезд",
            "shot_size": size, "location": where, "cast": list(cast), "source": source,
            "photo_ref": ref, "on_screen_text": text}


def good_script(total=20):
    """Сценарий, который проходит правила: события, разные крупности, есть героиня."""
    return {"title": "Ранок", "key_shot": 3, "cast": [OLENA], "shots": [
        shot(1, 2.5, "hook", size="close", where="салон авто", cast=["Олена"]),
        shot(2, 4, "problem", size="wide", where="пробка"),
        shot(3, 5, "solution", size="close", where="кухня"),
        shot(4, 5, "benefit", size="macro", where="кухня"),
        shot(5, total - 16.5, "cta", text="Замовляй на сайті", source="end_card",
             where="студія")]}


RULES = validate.ScriptRules.for_duration(20, {11, 12}, model_max=15)


# ---------- правила ----------

def test_good_script_passes_and_normalize_fixes_small_gaps():
    s = VideoScript.model_validate(good_script())
    assert validate.check_script(s, RULES) == []
    off = VideoScript.model_validate(good_script())
    off.shots[1].duration_sec = 3.2                      # сумма 19.2 — дотянет сам
    validate.normalize_script(off, RULES)
    assert off.total == 20 and validate.check_script(off, RULES) == []


def test_script_rule_violations_are_explained():
    bad = good_script()
    bad["shots"][0]["role"] = "benefit"
    bad["shots"][-1]["role"] = "benefit"
    bad["shots"][2]["photo_ref"] = 99
    bad["shots"][3]["on_screen_text"] = "раз два три четыре пять шесть сім вісім"
    bad["key_shot"] = 1
    errors = validate.check_script(VideoScript.model_validate(bad), RULES)
    text = " | ".join(errors)
    for part in ("кадр 1 должен быть role=hook", "последний кадр", "photo_ref=99",
                 "максимум 7", "key_shot=1"):
        assert part in text


def story_errors(mutate) -> str:
    bad = good_script()
    mutate(bad)
    return " | ".join(validate.check_script(VideoScript.model_validate(bad), RULES))


def test_scene_must_have_an_event_not_a_camera_move():
    """Урок 22.10.2026: «медленный наезд на кружку» — это не событие, сцена пустая."""
    def camera_only(s):
        s["shots"][1]["event"] = "Медленный наезд на кружку"

    def empty(s):
        s["shots"][1]["event"] = ""

    def same(s):
        s["shots"][2]["event"] = s["shots"][1]["event"]

    assert "движение камеры, а не событие" in story_errors(camera_only)
    assert "заполни event" in story_errors(empty)
    assert "одно и то же событие" in story_errors(same)


def test_video_cannot_be_one_living_photo():
    def packshots(s):
        for sh in s["shots"][1:3]:
            sh["source"] = "product_photo"

    def product_only(s):
        s["cast"] = []
        for sh in s["shots"]:
            sh["cast"] = []

    def same_size(s):
        for sh in s["shots"]:
            sh["shot_size"] = "close"

    assert "оживает фото товара" in story_errors(packshots)
    assert "только товар" in story_errors(product_only)
    assert "крупности" in story_errors(same_size)


def test_jump_cut_and_cast_are_checked():
    def jump(s):
        s["shots"][3]["shot_size"] = "close"           # кадры 3 и 4: крупный, кухня, без героя

    def stranger(s):
        s["shots"][1]["cast"] = ["Петро"]

    def story_key(s):
        s["shots"][2]["source"] = "story_scene"

    assert "кадры 3 и 4" in story_errors(jump) and "скачком" in story_errors(jump)
    assert "героя «Петро» нет в cast" in story_errors(stranger)
    assert "не может быть story_scene" in story_errors(story_key)


def test_story_scene_needs_no_product_photo():
    s = good_script()
    s["shots"][1].update(source="story_scene", photo_ref=None)
    script = validate.normalize_script(VideoScript.model_validate(s), RULES)
    assert script.shots[1].photo_ref is None and not script.shots[1].has_product
    assert validate.check_script(script, RULES) == []


def test_directions_need_a_story():
    from adreel.script.schema import Directions

    flat = dict(DIRECTION, title="Красиво", beats=["товар крупно"], cast="", world="")
    errors = " | ".join(validate.check_directions(
        Directions.model_validate({"directions": [DIRECTION, flat,
                                                  dict(DIRECTION, title="Третє")]}), 3))
    assert "«Красиво»: распиши сюжет" in errors
    assert "заполни cast" in errors and "заполни world" in errors
    assert "Кава не холоне" not in errors


def voice(lines, claims=()):
    return Voiceover.model_validate({"lines": [{"n": n, "text": t} for n, t in lines],
                                     "claims": list(claims)})


def test_voiceover_limits_forbidden_and_sources():
    s = VideoScript.model_validate(good_script())
    ok = voice([(1, "Кава знову холодна?"), (2, ""), (3, "А тут — гаряча."),
                (4, "Тримає тепло 8 годин."), (5, "Замовляй на сайті.")],
               [{"text": "тримає тепло 8 годин", "source": "page:1"}])
    assert validate.check_voiceover(ok, s, CARD, chars_per_sec=15) == []

    bad = voice([(1, "Це найкраща в Україні кружка, яка гарантовано №1 серед усіх кружок"),
                 (3, "x"), (4, "y"), (5, "z")],
                [{"text": "перемога", "source": "інтернет"}])
    text = " | ".join(validate.check_voiceover(bad, s, CARD, chars_per_sec=15))
    for part in ("кадр 1:", "влезает", "нет строк озвучки для кадров [2]", "«№1»",
                 "«гарант»", "нельзя обещать", "не из карточки"):
        assert part in text


# ---------- переделка по ошибкам ----------

def test_market_research_reaches_the_prompt():
    """Поиск и выжимка платные — без этого блока они уходили в никуда."""
    brief = creative.Brief(CARD, [], [], market="РЫНОК И ЛЮДИ:\nБоли покупателей: кофе остывает")
    assert "Боли покупателей: кофе остывает" in creative.brief_message(brief, "Напиши.")
    assert "РЫНОК" not in creative.brief_block(creative.Brief(CARD, [], []))


async def test_agent_fixes_script_after_rule_errors():
    broken = good_script()
    broken["shots"][0]["role"] = "benefit"
    llm = FakeLLM([broken, good_script()])
    brief = creative.Brief(CARD, [creative.PhotoInfo(11, "кружка на столе")], ["хочу динамічно"])
    res = await creative.write_script(llm, brief,
                                      creative.Direction.model_validate(DIRECTION), RULES)
    assert res.shots[0].role == "hook"
    assert res.prompt_version == load("video_script").version
    assert len(llm.calls) == 2
    retry_user = llm.calls[1][1]
    assert "НЕ ПРОШЁЛ ПРОВЕРКУ" in retry_user and "role=hook" in retry_user
    # бриф — данные в рамке в user-сообщении, не в system (инъекция со страницы товара)
    assert llm.calls[1][1].startswith("<бриф>")       # и в переспросе тоже
    system, user = llm.calls[0][0], llm.calls[0][1]
    assert "хочу динамічно" not in system and "<бриф>" in system
    assert user.startswith("<бриф>") and "хочу динамічно" in user
    assert "11 — кружка на столе" in user


async def test_agent_gives_up_with_clear_error():
    broken = good_script()
    broken["key_shot"] = 1
    llm = FakeLLM([broken] * (creative.FIX_ROUNDS + 1))
    with pytest.raises(creative.ScriptError, match="key_shot"):
        await creative.write_script(llm, creative.Brief(CARD, [creative.PhotoInfo(11, "x")], []),
                                    creative.Direction.model_validate(DIRECTION), RULES)


async def test_directions_must_differ():
    same = {"directions": [DIRECTION, DIRECTION, DIRECTION, DIRECTION]}
    other = {"directions": [dict(DIRECTION, title=f"Напрям {i}") for i in range(4)]}
    llm = FakeLLM([same, other])
    res = await creative.propose_directions(llm, creative.Brief(CARD, [], []))
    assert len({d.title for d in res.directions}) == 4
    assert "повторяются" in llm.calls[1][1]


# ---------- путь целиком ----------

@pytest.fixture
async def world(tmp_path):
    url = f"sqlite+aiosqlite:///{(tmp_path / 's.db').as_posix()}"
    upgrade(url)
    engine = make_engine(url)
    sessions = make_sessions(engine)
    media = MediaStore(tmp_path)
    products = ProductService(Deps(sessions=sessions, media=media, text_llm=lambda: None,
                                   vision_llm=lambda: None, stt=lambda: None))
    llm = FakeLLM()
    scripts = ScriptService(sessions, products, lambda: llm, chars_per_sec=lambda: 15.0)
    yield products, scripts, llm, MusicService(sessions, media), sessions
    await engine.dispose()


async def test_direction_script_voiceover_then_music_under_script(world):
    products, scripts, llm, music, sessions = world
    pid = await products.new_draft(1, "uk")
    await products.save_card(pid, CARD)
    photo = await products.add_photo(pid, solid_png(900, 900))
    assert not photo.duplicate
    photo_id = (await products.product_photos(pid))[0].id
    project_id = await scripts.start_project(pid)

    llm.responses = [{"directions": [dict(DIRECTION, title=f"Напрям {i}") for i in range(4)]}]
    dirs = await scripts.directions(project_id)
    await scripts.choose_direction(project_id, dirs.directions[0])
    await scripts.set_duration(project_id, 20)

    script = good_script()
    for sh in script["shots"]:
        sh["photo_ref"] = photo_id
    llm.responses = [script]
    s = await scripts.make_script(project_id)
    assert s.total == 20

    llm.responses = [{"lines": [{"n": i, "text": "Кава гаряча."} for i in range(1, 6)],
                      "claims": []}]
    vo = await scripts.make_voiceover(project_id)
    assert vo.lines[0].text_tts == "Кава гаряча."          # пустой text_tts дополнен

    c = await scripts.get(project_id)
    assert c.direction and c.script and c.voiceover

    # новый сценарий сбрасывает устаревшую озвучку
    llm.responses = [script]
    await scripts.make_script(project_id, "variant")
    assert (await scripts.get(project_id)).voiceover is None
    assert "ДРУГОЙ вариант" in llm.calls[-1][1]

    # музыка под сценарий: дроп на главный кадр (кадр 3 начинается на 6.5 с)
    mmap = analyze(click_track(60, drop_at=30))
    async with sessions() as ses:
        t = Track(owner_id=1, title="beat", file_path="x.mp3", sha256="0" * 64,
                  duration=mmap.duration, bpm=mmap.bpm, analysis=mmap.model_dump())
        ses.add(t)
        await ses.commit()
        track_id = t.id
    choice = await music.choose(project_id, track_id)
    assert choice.window.end - choice.window.start == pytest.approx(20)
    assert choice.window.drop_at == pytest.approx(6.5, abs=1.0)

    custom, note = await music.choose_custom(project_id, track_id, "0:10–0:40")
    assert custom.window.end - custom.window.start == pytest.approx(20)
    assert "под сценарий" in note
    async with sessions() as ses:
        assert (await ses.get(Project, project_id)).duration_sec == 20
