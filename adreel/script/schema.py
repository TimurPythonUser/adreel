"""Креатив ролика: направление → сценарий видео (кадры) → озвучка под кадры."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

ShotRole = Literal["hook", "problem", "solution", "benefit", "demo", "proof", "offer", "cta"]
ShotSource = Literal["product_photo", "generated_scene", "end_card"]


class Direction(BaseModel):
    """Одно креативное направление ролика."""

    title: str = Field(description="название направления, 2–4 слова")
    format: str = Field(description="формат ролика: «проблема → решение», «мини-история» и т.п.")
    hook: str = Field(description="что происходит в первые 2 секунды, чтобы не пролистали")
    idea: str = Field(description="суть ролика, 2–3 предложения")
    visual_style: str = Field(description="свет, камера, цвета, фон, темп монтажа")
    voice_tone: str = Field(description="как звучит диктор")
    music_mood: str = Field(description="какая музыка подойдёт")
    why_it_works: str = Field(description="почему это продаст этот товар, 1 предложение")
    risks: str = Field(default="", description="что может не получиться у видеонейросети")


class Directions(BaseModel):
    directions: list[Direction] = Field(min_length=1, max_length=5)


class Shot(BaseModel):
    n: int = Field(description="номер кадра с 1")
    duration_sec: float
    role: ShotRole
    visual: str = Field(description="что в кадре: объект, действие, окружение, свет — "
                                    "подробно, по этому описанию генерируется картинка")
    camera: str = Field(description="движение камеры и объекта за кадр")
    source: ShotSource = Field(description="product_photo — реальное фото товара как основа; "
                                           "generated_scene — сцена с товаром по референсу; "
                                           "end_card — финальная карточка")
    photo_ref: int | None = Field(default=None, description="id фото товара из списка")
    on_screen_text: str = Field(default="", description="надпись на экране, до 7 слов")
    transition: str = Field(default="cut", description="переход к следующему кадру")


class VideoScript(BaseModel):
    title: str
    key_shot: int = Field(description="номер главного кадра — раскрытие товара, ляжет на дроп")
    shots: list[Shot] = Field(min_length=2, max_length=8)
    prompt_version: str = ""

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
