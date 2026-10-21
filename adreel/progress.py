"""Прогресс долгих шагов: колбэк обновляет статус в Telegram, шагу всё равно, есть ли он."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

Progress = Callable[[str], Awaitable[None]]


async def say(progress: Progress | None, text: str) -> None:
    if progress is not None:
        await progress(text)
