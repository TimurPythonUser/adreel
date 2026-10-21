"""Промпты лежат в prompts/*.md. Первая строка — версия: <!-- version: имя_vN -->.

Подстановка — {{переменная}}, а не str.format: в промптах полно JSON-примеров
с фигурными скобками.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from .config import settings

_VERSION = re.compile(r"<!--\s*version:\s*([\w.-]+)\s*-->")
_VAR = re.compile(r"\{\{\s*(\w+)\s*\}\}")


@dataclass(frozen=True)
class Prompt:
    name: str
    version: str
    text: str


@lru_cache(maxsize=32)
def load(name: str) -> Prompt:
    raw = (settings.prompts_dir / f"{name}.md").read_text(encoding="utf-8")
    m = _VERSION.search(raw.splitlines()[0] if raw else "")
    if not m:
        raise ValueError(f"prompts/{name}.md: первой строкой нужен <!-- version: ... -->")
    body = raw.split("\n", 1)[1] if "\n" in raw else ""
    return Prompt(name, m.group(1), body.strip())


def render(name: str, **values: object) -> str:
    prompt = load(name)

    def sub(m: re.Match[str]) -> str:
        key = m.group(1)
        if key not in values:
            raise KeyError(f"prompts/{name}.md: не передана переменная {key}")
        return str(values[key])

    return _VAR.sub(sub, prompt.text)
