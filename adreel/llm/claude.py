"""Claude: схема ответа гарантируется на стороне API (messages.parse + Pydantic)."""

from __future__ import annotations

import logging
from typing import Any, cast

from anthropic import AsyncAnthropic

from .. import costs
from .base import Content, LLMError, T

log = logging.getLogger("adreel.llm.claude")


class ClaudeLLM:
    def __init__(self, model: str, api_key: str, client: Any | None = None) -> None:
        self.model = model
        # SDK сам повторяет 429/5xx и сетевые сбои — в net.py Claude не заводим.
        self.client = client or AsyncAnthropic(api_key=api_key or None)

    async def parse(self, system: str, content: Content, output: type[T],
                    max_tokens: int = 16000) -> T:
        resp = await self.client.messages.parse(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            # Content — наш упрощённый тип блоков; по форме он совпадает с MessageParam SDK.
            messages=[{"role": "user", "content": cast(Any, content)}],
            output_format=output,
        )
        usage = getattr(resp, "usage", None)
        if usage is not None:
            costs.record_llm("anthropic", self.model, usage.input_tokens, usage.output_tokens)

        if resp.stop_reason == "refusal":
            raise LLMError(f"{self.model} отказался отвечать на запрос")
        if resp.stop_reason == "max_tokens":
            # Обрезанный JSON не валиден — лучше честная ошибка, чем тихий повтор за деньги.
            raise LLMError(f"{self.model}: ответ обрезан по max_tokens={max_tokens}, подними лимит")
        parsed = resp.parsed_output
        if parsed is None:
            raise LLMError(f"{self.model} не вернул ответ по схеме {output.__name__}")
        return parsed  # type: ignore[no-any-return]
