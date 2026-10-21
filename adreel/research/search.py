"""Веб-поиск через Tavily: чем живёт рынок, как люди говорят о таком товаре.

ЗАЧЕМ. Креативщик без рынка предлагает общие идеи. Поиск даёт боли покупателей,
живые формулировки и форматы, которые сейчас заходят.

ГРАНИЦА. Найденное в интернете — материал ДЛЯ ИДЕЙ И ФОРМУЛИРОВОК. Факты о товаре
(состав, цена, свойства) берутся только из карточки: чужой сайт про чужой товар
в нашу рекламу попасть не должен.

Ключа нет — просто работаем без поиска: шаг необязательный.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from .. import costs, net
from ..config import load_yaml, settings

URL = "https://api.tavily.com/search"
log = logging.getLogger("adreel.research.search")


@dataclass(frozen=True)
class SearchHit:
    title: str
    url: str
    content: str


class SearchProvider(Protocol):
    async def search(self, query: str, *, max_results: int = 5) -> list[SearchHit]: ...


class TavilySearch:
    def __init__(self, api_key: str, depth: str = "basic") -> None:
        self.api_key = api_key
        self.depth = depth

    async def search(self, query: str, *, max_results: int = 5) -> list[SearchHit]:
        r = await net.request(
            "POST", URL, who="Tavily", timeout=60, retry_429=False,
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={"query": query, "max_results": max_results, "search_depth": self.depth},
        )
        if r.status_code in (401, 429):
            raise net.ServiceUnavailable(f"Tavily {r.status_code}: ключ или лимит запросов")
        r.raise_for_status()
        body = r.json()
        credits = float((body.get("usage") or {}).get("credits") or 1)
        prices = load_yaml("pricing.yaml").get("tavily") or {}
        costs.record("tavily", "search", credits, "credits",
                     credits * float(prices.get("usd_per_credit", 0.0)))
        return [SearchHit(str(h.get("title", "")), str(h.get("url", "")),
                          str(h.get("content", ""))[:1200])
                for h in body.get("results", [])]


def make_search() -> SearchProvider | None:
    """None — поиска нет (нет ключа). Креатив работает и без него."""
    if not settings.tavily_api_key:
        return None
    return TavilySearch(settings.tavily_api_key)
