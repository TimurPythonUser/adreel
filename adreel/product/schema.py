"""Карточка товара — всё, что сценарист знает о товаре.

Правило фактов: у каждого преимущества и характеристики есть источник. Сценарий
(этап 3) опирается только на факты с источником — так в рекламу не попадают
выдуманные обещания.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Language = Literal["uk", "ru", "en"]

#: поля, про которые бот может спросить; порядок = порядок вопросов
QUESTION_FIELDS = (
    "name", "what_it_is", "audience", "benefits", "price", "offer",
    "forbidden_claims", "tone", "cta",
)


class Fact(BaseModel):
    text: str
    # откуда факт: "page:3" (материал №3), "photo:5", "user" (ответ владельца)
    source: str


class Offer(BaseModel):
    text: str
    ends_at: str | None = Field(default=None, description="YYYY-MM-DD, если срок известен")


class ProductCard(BaseModel):
    language: Language = "uk"
    name: str = ""
    brand: str = ""
    category: str = Field(default="other", description="ключ из списка категорий")
    what_it_is: str = Field(default="", description="что это и зачем, 1-2 предложения")
    audience: str = ""
    benefits: list[Fact] = Field(default_factory=list)
    specs: list[Fact] = Field(default_factory=list)
    price: str = Field(default="", description="как в объявлении, например '899 грн'")
    offer: Offer | None = None
    forbidden_claims: list[str] = Field(default_factory=list)
    tone: str = ""
    cta: str = ""
    notes: str = Field(default="", description="что ещё важно учесть в рекламе")
    # Поля, на которые владелец уже ответил (в т.ч. «нет акции», «без ограничений»).
    # Про них больше не спрашиваем, и LLM не имеет права их перетирать.
    answered: list[str] = Field(default_factory=list)

    def mark_answered(self, field: str) -> None:
        if field not in self.answered:
            self.answered.append(field)


class Suggestions(BaseModel):
    """Варианты для кнопок: владельцу проще ткнуть, чем печатать."""

    audience: list[str] = Field(default_factory=list, max_length=3)
    benefits: list[str] = Field(default_factory=list, max_length=5)
    tone: list[str] = Field(default_factory=list, max_length=4)
    cta: list[str] = Field(default_factory=list, max_length=3)


class CardExtraction(BaseModel):
    """Ответ LLM при сборке карточки из материалов."""

    card: ProductCard
    suggestions: Suggestions = Field(default_factory=Suggestions)
    doubts: list[str] = Field(default_factory=list,
                              description="что в материалах противоречит или неясно")


class ImageNote(BaseModel):
    index: int
    is_product: bool = Field(description="на фото сам товар (а не логотип, баннер, мусор)")
    description: str
    visible_text: str = Field(default="", description="надписи, которые видно на фото")
    quality: Literal["good", "ok", "low"] = "ok"


class ImageNotes(BaseModel):
    notes: list[ImageNote]
