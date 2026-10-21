"""Подделка LLM: отдаёт заранее заготовленные ответы по очереди."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from .base import Content, LLMError, T


@dataclass
class FakeLLM:
    responses: list[dict[str, Any] | BaseModel] = field(default_factory=list)
    model: str = "fake"
    calls: list[tuple[str, Content]] = field(default_factory=list)

    async def parse(self, system: str, content: Content, output: type[T],
                    max_tokens: int = 16000) -> T:
        self.calls.append((system, content))
        if not self.responses:
            raise LLMError("FakeLLM: заготовленные ответы кончились")
        nxt = self.responses.pop(0)
        data = nxt.model_dump() if isinstance(nxt, BaseModel) else nxt
        return output.model_validate(data)
