"""JSON по схеме для моделей без гарантии схемы (DeepSeek): валидация + переспрос.

Перенесено из AI_Host (videonews/llm.py). DeepSeek на «отвечай JSON-объектом»
реально возвращает битый JSON (проверено), поэтому:
  • схема кладётся в системный промпт;
  • ответ валидируется Pydantic;
  • при ошибке модель переспрашивается с текстом ошибки;
  • при обрыве по лимиту (finish=length) лимит удваивается — тот же промпт просто не влез.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable

from pydantic import BaseModel, ValidationError

from .base import LLMError, T

log = logging.getLogger("adreel.llm.repair")

#: (messages, max_tokens) -> (текст ответа, finish_reason)
Call = Callable[[list[dict[str, str]], int], Awaitable[tuple[str, str | None]]]

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def strip_fence(text: str) -> str:
    """Модель иногда оборачивает JSON в ```json ... ``` вопреки инструкции."""
    return _FENCE.sub("", text.strip())


def json_rules(output: type[BaseModel]) -> str:
    schema = json.dumps(output.model_json_schema(), ensure_ascii=False)
    return (
        "\n\nФОРМАТ ОТВЕТА (строго): верни РОВНО ОДИН валидный JSON-объект по этой схеме. "
        "Без markdown, без ```, без пояснений до или после. Все обязательные поля "
        f"должны быть на месте.\nСхема:\n{schema}"
    )


async def parse_with_repair(
    call: Call, *, system: str, user: str, output: type[T], max_tokens: int,
    who: str, attempts: int = 3,
) -> T:
    messages = [
        {"role": "system", "content": system + json_rules(output)},
        {"role": "user", "content": user},
    ]
    last: Exception | None = None
    for attempt in range(attempts):
        raw, finish = await call(messages, max_tokens)
        try:
            return output.model_validate(json.loads(strip_fence(raw)))
        except (json.JSONDecodeError, ValidationError) as exc:
            last = exc
            if finish == "length":
                max_tokens *= 2
                log.warning("%s: ответ обрезан по лимиту, поднимаю max_tokens до %d",
                            who, max_tokens)
                continue
            log.warning("%s: попытка %d не прошла проверку (%s), переспрашиваю",
                        who, attempt + 1, type(exc).__name__)
            messages += [
                {"role": "assistant", "content": raw[:4000]},
                {"role": "user", "content": (
                    f"Твой ответ не прошёл проверку: {str(exc)[:500]}\n"
                    "Верни ВЕСЬ объект заново — только валидный JSON по схеме, "
                    "без markdown и без пояснений."
                )},
            ]
    raise LLMError(f"{who} не вернул валидный JSON за {attempts} попытки: {str(last)[:300]}")
