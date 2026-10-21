"""Рыночная выжимка для креативщика: боли, возражения, живые формулировки, тренды."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, Field

from .. import prompts
from ..llm.base import LLMProvider
from ..product.extract import categories
from ..product.schema import ProductCard
from .search import SearchProvider

log = logging.getLogger("adreel.research.notes")

#: выжимка живёт месяц: рынок меняется медленнее, чем мы делаем ролики
TTL = timedelta(days=30)
MAX_HITS_CHARS = 9000


class ResearchNotes(BaseModel):
    pains: list[str] = Field(default_factory=list, max_length=6)
    desires: list[str] = Field(default_factory=list, max_length=6)
    objections: list[str] = Field(default_factory=list, max_length=5)
    hooks: list[str] = Field(default_factory=list, max_length=6)
    trends: list[str] = Field(default_factory=list, max_length=5)
    phrases: list[str] = Field(default_factory=list, max_length=8)
    sources: list[str] = Field(default_factory=list, max_length=6)
    fetched_at: str = ""

    @property
    def empty(self) -> bool:
        return not (self.pains or self.desires or self.hooks or self.phrases)

    def fresh(self) -> bool:
        try:
            return datetime.fromisoformat(self.fetched_at) > datetime.now(UTC) - TTL
        except ValueError:
            return False

    def block(self) -> str:
        def row(title: str, items: list[str]) -> str:
            return f"{title}: " + "; ".join(items) if items else ""

        rows = [row("Боли покупателей", self.pains), row("Чего хотят", self.desires),
                row("Возражения", self.objections), row("Рабочие хуки", self.hooks),
                row("Тренды формата", self.trends), row("Как говорят люди", self.phrases)]
        body = "\n".join(r for r in rows if r)
        if not body:
            return ""
        return ("РЫНОК И ЛЮДИ (из интернета — для идей и формулировок, НЕ для фактов "
                f"о товаре):\n{body}")


def queries(card: ProductCard) -> list[str]:
    what = card.what_it_is or card.name
    cat = categories().get(card.category, {}).get("title", "")
    return [q for q in (
        f"{card.name} отзывы покупателей что не нравится" if card.name else "",
        f"{what} почему выбирают как выбрать",
        f"{cat} реклама tiktok reels хуки что цепляет 2026",
    ) if q.strip()]


async def collect(search: SearchProvider, llm: LLMProvider, card: ProductCard) -> ResearchNotes:
    hits = []
    for q in queries(card):
        try:
            hits += await search.search(q, max_results=5)
        except Exception as exc:  # поиск не критичен — работаем без него
            log.warning("поиск «%s» не удался: %s", q, str(exc)[:150])
    if not hits:
        return ResearchNotes(fetched_at=datetime.now(UTC).isoformat())

    found, budget = [], MAX_HITS_CHARS
    for h in hits:
        chunk = f"[{h.url}] {h.title}\n{h.content}"[:budget]
        if chunk:
            found.append(chunk)
            budget -= len(chunk)
        if budget <= 0:
            break
    product = f"{card.name} — {card.what_it_is} ({card.category})"
    system = prompts.render("research_notes", language=card.language, product=product)
    notes = await llm.parse(system, "НАЙДЕНО В ИНТЕРНЕТЕ:\n\n" + "\n\n".join(found),
                            ResearchNotes, max_tokens=4000)
    notes.fetched_at = datetime.now(UTC).isoformat()
    return notes
