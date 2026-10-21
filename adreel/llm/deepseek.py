"""DeepSeek (текст, без зрения): json_object + схема в промпте + переспрос (repair.py)."""

from __future__ import annotations

from .. import costs, net
from .base import Content, LLMError, T, has_images
from .repair import parse_with_repair

BASE = "https://api.deepseek.com"


class DeepSeekLLM:
    def __init__(self, model: str, api_key: str) -> None:
        if not api_key:
            raise LLMError("не задан DEEPSEEK_API_KEY (.env)")
        self.model = model
        self.api_key = api_key

    async def _call(
        self, messages: list[dict[str, str]], max_tokens: int
    ) -> tuple[str, str | None]:
        r = await net.request(
            "POST", f"{BASE}/chat/completions", who="DeepSeek",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={"model": self.model, "messages": messages,
                  "response_format": {"type": "json_object"}, "max_tokens": max_tokens},
        )
        if r.status_code == 401:
            raise LLMError("DeepSeek 401: неверный DEEPSEEK_API_KEY")
        if r.status_code == 402:
            raise LLMError("DeepSeek 402: кончились деньги на балансе")
        r.raise_for_status()
        body = r.json()
        u = body.get("usage") or {}
        costs.record_llm("deepseek", self.model,
                         u.get("prompt_tokens", 0), u.get("completion_tokens", 0))
        choice = body["choices"][0]
        return choice["message"]["content"] or "", choice.get("finish_reason")

    async def parse(self, system: str, content: Content, output: type[T],
                    max_tokens: int = 16000) -> T:
        if has_images(content):
            raise LLMError(f"{self.model} не принимает картинки — для зрения нужна Claude-модель "
                           "в VISION_MODEL")
        user = content if isinstance(content, str) else "\n\n".join(
            b["text"] for b in content if b.get("type") == "text")
        return await parse_with_repair(self._call, system=system, user=user, output=output,
                                       max_tokens=max_tokens, who=self.model)
