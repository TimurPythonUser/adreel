"""LLM: выбор реализации по имени модели."""

from __future__ import annotations

from ..config import settings
from .base import LLMError, LLMProvider, image_block, text_block
from .claude import ClaudeLLM
from .deepseek import DeepSeekLLM

__all__ = ["LLMError", "LLMProvider", "image_block", "make_llm", "text_block"]


def make_llm(model: str) -> LLMProvider:
    """Всё, что начинается с "deepseek", идёт в DeepSeek, остальное — в Claude."""
    if not model:
        raise LLMError("имя модели пустое — задай TEXT_MODEL / VISION_MODEL в .env")
    if model.lower().startswith("deepseek"):
        return DeepSeekLLM(model, settings.deepseek_api_key)
    if not settings.anthropic_api_key:
        raise LLMError("не задан ANTHROPIC_API_KEY (.env)")
    return ClaudeLLM(model, settings.anthropic_api_key)


def text_llm() -> LLMProvider:
    return make_llm(settings.text_model)


def vision_llm() -> LLMProvider:
    return make_llm(settings.vision_model)


def creative_llm() -> LLMProvider:
    return make_llm(settings.creative_model or settings.text_model)
