"""Интерфейс LLM: запрос со структурированным ответом (Pydantic-модель)."""

from __future__ import annotations

import base64
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

#: строка или блоки контента в формате Messages API (текст + картинки)
Content = str | list[dict[str, Any]]


class LLMError(RuntimeError):
    pass


class LLMProvider(Protocol):
    model: str

    async def parse(self, system: str, content: Content, output: type[T],
                    max_tokens: int = 16000) -> T: ...


def text_block(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def image_block(data: bytes, media_type: str) -> dict[str, Any]:
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": media_type,
                   "data": base64.b64encode(data).decode("ascii")},
    }


def has_images(content: Content) -> bool:
    return not isinstance(content, str) and any(b.get("type") == "image" for b in content)
