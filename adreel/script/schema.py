"""Креатив ролика: направление → сценарий видео (кадры) → озвучка под кадры."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

ShotRole = Literal["hook", "problem", "solution", "benefit", "demo", "proof", "offer", "cta"]
#: product_photo — реальное фото товара оживает (крупный план, максимум один на ролик);
#: generated_scene — товар в новой сцене (картинку рисуем по фото товара);
#: story_scene — сцена БЕЗ товара: человек, его мир, проблема, реакция;
#: end_card — финальная карточка
ShotSource = Literal["product_photo", "generated_scene", "story_scene", "end_card"]
ShotSize = Literal["wide", "medium", "close", "macro"]


class Direction(BaseModel):
    """Одно креативное направление ролика."""

    title: str = Field(description="название направления, 2–4 слова")
    format: str = Field(description="формат ролика: «проблема → решение», «мини-история» и т.п.")
    hook: str = Field(description="что происходит в первые 2 секунды, чтобы не пролистали")
    idea: str = Field(description="суть ролика, 2–3 предложения")
    # Сюжет по шагам — главное поле. Без него направления выходили «товар красиво»,
    # и режиссёру нечего было снимать, кроме наездов на товар.
    beats: list[str] = Field(default_factory=list,
                             description="сюжет по шагам, 3–5 событий: что ПРОИСХОДИТ в кадре "
                                         "на каждом шаге (действие героя, предмета, среды)")
    cast: str = Field(default="", description="кто в кадре: герой (возраст, внешность, одежда) "
                                              "или «без людей, только руки»")
    world: str = Field(default="", description="где происходит: 1–3 локации и ключевые предметы")
    visual_style: str = Field(description="свет, камера, цвета, фон, темп монтажа")
    voice_tone: str = Field(description="как звучит диктор")
    music_mood: str = Field(description="какая музыка подойдёт")
    why_it_works: str = Field(description="почему это продаст этот товар, 1 предложение")
    risks: str = Field(default="", description="что может не получиться у видеонейросети")


class Directions(BaseModel):
    directions: list[Direction] = Field(min_length=1, max_length=5)


class CastMember(BaseModel):
    """Герой ролика. Описание повторяется слово в слово во всех его кадрах, а первая
    его картинка уходит референсом в следующие — так модель держит одного человека."""

    name: str = Field(description="короткое имя для ссылок из сцен: «Олена», «водитель»")
    look: str = Field(description="внешность одной фразой: возраст, лицо, волосы, одежда — "
                                  "конкретно, без «красивый»")


class Shot(BaseModel):
    n: int = Field(description="номер кадра с 1")
    duration_sec: float
    role: ShotRole
    visual: str = Field(description="стартовый кадр сцены: кто и что в кадре, где, свет, "
                                    "ракурс — подробно, по этому описанию рисуется картинка")
    event: str = Field(default="", description="что ПРОИСХОДИТ за сцену: одно действие героя, "
                                               "предмета или среды, из состояния А в Б. "
                                               "Движение камеры — не событие")
    camera: str = Field(description="движение камеры за кадр")
    shot_size: ShotSize = Field(default="medium", description="крупность: wide — общий, "
                                                              "medium — средний, close — "
                                                              "крупный, macro — деталь")
    location: str = Field(default="", description="где сцена, 1–3 слова: «кухня», «салон авто»")
    cast: list[str] = Field(default_factory=list, description="имена героев в кадре из cast")
    source: ShotSource = Field(description="product_photo — оживает реальное фото товара; "
                                           "generated_scene — товар в новой сцене; "
                                           "story_scene — сцена без товара; "
                                           "end_card — финальная карточка")
    photo_ref: int | None = Field(default=None, description="id фото товара из списка; "
                                                            "у story_scene — пусто")
    on_screen_text: str = Field(default="", description="надпись на экране, до 7 слов")
    transition: str = Field(default="cut", description="переход к следующему кадру")

    @property
    def has_product(self) -> bool:
        return self.source != "story_scene"


class VideoScript(BaseModel):
    title: str
    key_shot: int = Field(description="номер главного кадра — раскрытие товара, ляжет на дроп")
    cast: list[CastMember] = Field(default_factory=list,
                                   description="герои ролика, 0–2 человека; руки тоже герой")
    shots: list[Shot] = Field(min_length=2, max_length=8)
    prompt_version: str = ""

    def member(self, name: str) -> CastMember | None:
        return next((c for c in self.cast if c.name == name), None)

    @property
    def total(self) -> float:
        return round(sum(s.duration_sec for s in self.shots), 2)

    def start_of(self, n: int) -> float:
        t = 0.0
        for s in self.shots:
            if s.n == n:
                return round(t, 2)
            t += s.duration_sec
        return 0.0


class Claim(BaseModel):
    text: str
    source: str = Field(description="источник из карточки: page:ID, photo:ID, user и т.п.")


class VoiceLine(BaseModel):
    n: int = Field(description="номер кадра")
    text: str = Field(description="что говорит диктор на этом кадре; пусто — пауза")
    text_tts: str = Field(default="", description="то же для синтеза: числа и цены словами")


class Voiceover(BaseModel):
    lines: list[VoiceLine]
    claims: list[Claim] = Field(default_factory=list,
                                description="утверждения о товаре из текста и их источники")
    prompt_version: str = ""

    @property
    def full_text(self) -> str:
        return " ".join(line.text for line in self.lines if line.text.strip())
