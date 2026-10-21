"""Какие вопросы задать владельцу и как применить ответ к карточке.

Вопросы детерминированные: LLM только предлагает варианты для кнопок. Так бот
не спрашивает то, что уже знает, и не задаёт больше 8 вопросов.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from dataclasses import field as dc_field

from .schema import QUESTION_FIELDS, Fact, Offer, ProductCard, Suggestions

MAX_QUESTIONS = 8
#: столько преимуществ нужно сценаристу, чтобы было из чего выбирать углы
MIN_BENEFITS = 3

DEFAULT_TONES = ["дружелюбный", "энергичный", "премиальный", "с юмором"]

#: «ответ-отказ» для полей, где «нет» — тоже ответ
NO_OFFER = "нет акции"
NO_LIMITS = "ограничений нет"
SKIP = "пропустить"


@dataclass
class Question:
    field: str
    text: str
    options: list[str] = dc_field(default_factory=list)
    #: можно ли ответить «пропустить» (поле необязательное)
    skippable: bool = False


def _need(card: ProductCard, name: str) -> bool:
    if name in card.answered:
        return False
    match name:
        case "benefits":
            return len(card.benefits) < MIN_BENEFITS
        case "offer":
            return card.offer is None
        case "forbidden_claims":
            return not card.forbidden_claims
        case _:
            return not str(getattr(card, name) or "").strip()


def pending_questions(card: ProductCard, sug: Suggestions | None = None) -> list[Question]:
    sug = sug or Suggestions()
    out: list[Question] = []
    for name in QUESTION_FIELDS:
        if not _need(card, name):
            continue
        out.append(_question(name, card, sug))
        if len(out) >= MAX_QUESTIONS:
            break
    return out


def _question(name: str, card: ProductCard, sug: Suggestions) -> Question:
    match name:
        case "name":
            return Question(name, "Как называется товар?")
        case "what_it_is":
            return Question(name, "Что это и зачем? Одним-двумя предложениями.")
        case "audience":
            return Question(name, "Для кого товар? Выбери или напиши своими словами.",
                            sug.audience[:3])
        case "benefits":
            have = "\n".join(f"• {b.text}" for b in card.benefits)
            text = "Назови главные плюсы товара — каждый с новой строки."
            if have:
                text = f"Уже знаю:\n{have}\n\nДобавь ещё плюсы — каждый с новой строки."
            options = [s for s in sug.benefits if s not in {b.text for b in card.benefits}]
            return Question(name, text, options[:4], skippable=bool(card.benefits))
        case "price":
            return Question(name, "Цена? Напиши как в объявлении, например «899 грн».",
                            skippable=True)
        case "offer":
            return Question(
                name,
                "Есть акция со сроком? Напиши, например «−20% до 20.09». "
                "Срочность в рекламе — только при реальном сроке.",
                [NO_OFFER])
        case "forbidden_claims":
            return Question(name, "Что нельзя обещать или говорить в рекламе? "
                                  "Каждое с новой строки.", [NO_LIMITS])
        case "tone":
            return Question(name, "Какой тон у ролика?", (sug.tone or DEFAULT_TONES)[:4])
        case "cta":
            return Question(name, "Призыв в конце ролика (CTA)?", sug.cta[:3])
    raise ValueError(f"нет вопроса для поля {name}")


def _lines(text: str) -> list[str]:
    parts = re.split(r"[\n;]+|(?:^|\s)[•\-–—]\s+", text)
    return [p.strip(" .,•-–—") for p in parts if p and p.strip(" .,•-–—")]


_DATE = re.compile(r"(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?")


def parse_deadline(text: str, year: int) -> str | None:
    """«до 20.09» -> "YYYY-09-20". Год по умолчанию — текущий."""
    m = _DATE.search(text)
    if not m:
        return None
    day, month = int(m.group(1)), int(m.group(2))
    y = m.group(3)
    yy = int(y) + 2000 if y and len(y) == 2 else int(y) if y else year
    if not (1 <= day <= 31 and 1 <= month <= 12):
        return None
    return f"{yy:04d}-{month:02d}-{day:02d}"


def apply_answer(card: ProductCard, name: str, answer: str, *, year: int) -> ProductCard:
    """Применить ответ владельца. Ответ владельца — факт с источником "user"."""
    card = card.model_copy(deep=True)
    value = answer.strip()
    card.mark_answered(name)
    if value.lower() == SKIP:
        return card
    match name:
        case "benefits":
            known = {b.text.lower() for b in card.benefits}
            for line in _lines(value):
                if line.lower() not in known:
                    card.benefits.append(Fact(text=line, source="user"))
                    known.add(line.lower())
        case "offer":
            if value.lower() == NO_OFFER:
                card.offer = None
            else:
                card.offer = Offer(text=value, ends_at=parse_deadline(value, year))
        case "forbidden_claims":
            if value.lower() != NO_LIMITS:
                card.forbidden_claims = [*card.forbidden_claims, *_lines(value)]
        case "name" | "what_it_is" | "audience" | "price" | "tone" | "cta":
            setattr(card, name, value)
        case _:
            raise ValueError(f"поле {name} не отвечается вопросом")
    return card
