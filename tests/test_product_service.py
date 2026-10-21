"""Сервис товара целиком на фейках: БД настоящая (SQLite + миграции), сети и LLM нет."""

from __future__ import annotations

from datetime import date

import pytest

from adreel.config import settings
from adreel.db.session import make_engine, make_sessions, upgrade
from adreel.gen.fake import solid_png
from adreel.ingest import pages
from adreel.ingest.media import MediaStore
from adreel.llm.fake import FakeLLM
from adreel.product.service import Deps, ProductError, ProductService


class FakeSTT:
    def __init__(self, text: str = "Это кружка для офиса, хочу динамичный ролик") -> None:
        self.text = text

    async def transcribe(self, audio, filename, seconds, language=None):
        return self.text


PAGE = pages.PageData(
    url="https://shop.test/cup", title="Термокружка Steel", price="899 UAH",
    text="Держит тепло 8 часов. Объём 450 мл.",
    images=["https://shop.test/big.png", "https://shop.test/icon.png"],
)


def card_response(**over):
    card = {
        "language": "uk", "name": "Термокружка Steel", "category": "home",
        "what_it_is": "Кружка, що тримає тепло", "benefits": [
            {"text": "тримає тепло 8 годин", "source": "page:1"}],
        "specs": [{"text": "450 мл", "source": "page:1"}], "price": "899 UAH",
        "answered": [],
    }
    card.update(over)
    return {"card": card, "suggestions": {"tone": ["дружній"], "cta": ["Замовляй зараз"],
                                          "audience": ["офісні працівники"]},
            "doubts": []}


@pytest.fixture
async def svc(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "workdir", tmp_path)
    url = f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}"
    upgrade(url)
    engine = make_engine(url)
    vision = FakeLLM([{"notes": [{"index": 0, "is_product": True, "description": "кружка",
                                  "quality": "good"}]}])
    text = FakeLLM([card_response()])

    async def fetch_page(u):
        return PAGE

    async def fetch_image(u):
        return solid_png(900, 1200) if "big" in u else solid_png(64, 64)

    async def frames(data):
        return [solid_png(720, 1280, (1, 2, 3))]

    async def download_video(u):
        return b"fake-video"

    service = ProductService(Deps(
        sessions=make_sessions(engine), media=MediaStore(tmp_path),
        text_llm=lambda: text, vision_llm=lambda: vision, stt=lambda: FakeSTT(),
        fetch_page=fetch_page, fetch_image=fetch_image, video_frames=frames,
        download_video=download_video, today=lambda: date(2026, 9, 15),
    ))
    service.fakes = (text, vision)  # type: ignore[attr-defined]
    yield service
    await engine.dispose()


async def test_full_flow_new_product(svc):
    pid = await svc.new_draft(owner_id=1, language="uk")

    link = await svc.add_link(pid, "https://shop.test/cup")
    assert link.extra == 1                             # иконка 64px отброшена
    small = await svc.add_photo(pid, solid_png(500, 500, (9, 9, 9)))
    assert "маленькое" in small.note
    dup = await svc.add_photo(pid, solid_png(500, 500, (9, 9, 9)))
    assert dup.duplicate
    voice = await svc.add_voice(pid, b"ogg", "v.ogg", 3.0)
    assert "кружка" in voice
    assert await svc.counts(pid) == {"link": 1, "photo": 2, "voice": 1}

    text_llm, vision_llm = svc.fakes
    # номера фото в разборе = id материалов; подменяем ответ под реальные id
    photo_ids = [m.id for m in await svc.product_photos(pid)]
    vision_llm.responses = [{"notes": [
        {"index": i, "is_product": True, "description": "кружка", "quality": "good"}
        for i in photo_ids]}]

    res = await svc.analyze(pid)
    assert res.card.name == "Термокружка Steel"
    fields = [q.field for q in res.questions]
    assert fields[0] == "audience" and "tone" in fields and "cta" in fields
    tone_q = next(q for q in res.questions if q.field == "tone")
    assert tone_q.options == ["дружній"]

    # в промпт ушли источники материалов и разбор фото
    _, user_prompt = text_llm.calls[0]
    assert f"[photo:{photo_ids[0]}]" in user_prompt and "[voice:" in user_prompt

    card = await svc.answer(pid, "offer", "нет акции")
    assert "offer" in card.answered

    confirmed = await svc.confirm(pid)
    assert confirmed.name == "Термокружка Steel"
    assert [p.id for p in await svc.recent(1)] == [pid]


async def test_reanalysis_keeps_owner_answers_and_skips_seen_photos(svc):
    pid = await svc.new_draft(1, "uk")
    await svc.add_photo(pid, solid_png(1000, 1000))
    text_llm, vision_llm = svc.fakes
    photo_id = (await svc.product_photos(pid))[0].id
    vision_llm.responses = [{"notes": [{"index": photo_id, "is_product": True,
                                        "description": "кружка"}]}]
    await svc.analyze(pid)
    await svc.answer(pid, "tone", "с юмором")

    # модель при пересборке «забыла» тон и вернула свой — ответ владельца должен победить
    text_llm.responses = [card_response(tone="строгий")]
    res = await svc.analyze(pid)
    assert res.card.tone == "с юмором"
    assert len(vision_llm.calls) == 1                  # старое фото повторно не разбирали


async def test_video_link_goes_to_video_with_frames(svc):
    pid = await svc.new_draft(1, "uk")
    res = await svc.add_link(pid, "https://youtu.be/abc")
    assert res.kind == "video" and res.extra == 1
    assert await svc.counts(pid) == {"video": 1, "frame": 1}


async def test_link_failures_suggest_screenshots(svc):
    pid = await svc.new_draft(1, "uk")

    async def blocked(u):
        raise pages.PageBlocked("HTTP 403")

    async def empty(u):
        raise pages.PageEmpty("пусто")

    svc.d.fetch_page = blocked
    with pytest.raises(ProductError, match=r"(?s)не пускает ботов.*скриншот"):
        await svc.add_link(pid, "https://eva.ua/ua/pr1")
    svc.d.fetch_page = empty
    with pytest.raises(ProductError, match=r"(?s)скриптами.*скриншот"):
        await svc.add_link(pid, "https://makeup.com.ua/ua/product/1")
    with pytest.raises(ProductError, match="файлом"):
        await svc.add_link(pid, "https://www.instagram.com/reel/abc")
    assert await svc.counts(pid) == {}                 # неудачные ссылки не оставляют мусора


async def test_errors_are_user_facing(svc):
    pid = await svc.new_draft(1, "uk")
    with pytest.raises(ProductError, match="материалов нет"):
        await svc.analyze(pid)
    with pytest.raises(ProductError, match="не картинка"):
        await svc.add_photo(pid, b"nope")
    with pytest.raises(ProductError, match="названия"):
        await svc.confirm(pid)
