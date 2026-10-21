"""Подделка поиска для тестов: ни одного сетевого вызова."""

from __future__ import annotations

from dataclasses import dataclass, field

from .search import SearchHit


@dataclass
class FakeSearch:
    hits: list[SearchHit] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)

    async def search(self, query: str, *, max_results: int = 5) -> list[SearchHit]:
        self.queries.append(query)
        return self.hits[:max_results]
