"""Плейбук: проверенный опыт по AI-роликам, который получают агенты креатива.

Знания живут в config/playbook.yaml, а не в промптах: правку приёма делаем в одном
месте, и её сразу видят креативщик, режиссёр и оператор. Всё, что сюда попадает,
проверено — гайдами моделей, разборами практиков или нашими собственными граблями.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from ..config import load_yaml

TITLES = {
    "story": "СЮЖЕТ: ИСТОРИЯ, А НЕ НАБОР КРАСИВЫХ ПЛАНОВ",
    "coverage": "РАСКАДРОВКА: КАЖДАЯ СЦЕНА — СОБЫТИЕ",
    "model_can": "ЧТО ВИДЕОМОДЕЛЬ ДЕЛАЕТ ХОРОШО",
    "model_cannot": "ЧТО ВИДЕОМОДЕЛЬ НЕ ВЫТЯНЕТ — не строй на этом сцены",
    "transitions": "ЧЕМ СКЛЕИВАЮТ СЦЕНЫ",
    "rhythm": "РИТМ ВЕРТИКАЛЬНОГО РОЛИКА",
    "lessons": "УРОКИ",
}


@lru_cache(maxsize=1)
def data() -> dict[str, Any]:
    return load_yaml("playbook.yaml")


def block(*sections: str) -> str:
    """Нужные разделы плейбука текстом для промпта."""
    out: list[str] = []
    book = data()
    for name in sections or tuple(TITLES):
        items = book.get(name) or []
        if not items:
            continue
        out.append(TITLES.get(name, name.upper()))
        for item in items:
            if isinstance(item, dict):
                out.append(f"• {item.get('name', '')} — когда: {item.get('when', '')}; "
                           f"как: {item.get('how', '')}")
            else:
                out.append(f"• {item}")
        out.append("")
    return "\n".join(out).strip()
