"""Общее для обработчиков: фоновые задачи и текст ошибки для владельца."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable
from html import escape
from typing import Any

#: фоновые задачи держим ссылками, иначе сборщик мусора может прибить их на ходу
_tasks: set[asyncio.Task[Any]] = set()


def spawn(coro: Awaitable[Any]) -> asyncio.Task[Any]:
    task: asyncio.Task[Any] = asyncio.ensure_future(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


def error_text(exc: BaseException, expected: tuple[type[BaseException], ...],
               log: logging.Logger) -> str:
    """Ожидаемую ошибку показываем как есть, остальное — в лог и коротко владельцу.

    Режем ДО экранирования: срезанная посередине сущность («&am») ломает HTML, и Telegram
    отклоняет уже само сообщение об ошибке — владелец видит молчание."""
    if isinstance(exc, expected):
        return f"⚠️ {escape(str(exc))}"
    log.error("непредвиденная ошибка", exc_info=exc)
    return f"⚠️ Не получилось: <code>{escape(str(exc)[:300])}</code>"
