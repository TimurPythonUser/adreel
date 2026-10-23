"""Интерфейсы генерации. Реализации: fal.py (живая), fake.py (тесты)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..progress import Progress as Progress


@dataclass(frozen=True)
class ImageInput:
    data: bytes
    mime: str = "image/png"


class ImageGenProvider(Protocol):
    async def edit(
        self,
        images: list[ImageInput],
        prompt: str,
        aspect_ratio: str | None = None,
        progress: Progress | None = None,
    ) -> bytes:
        """Новая картинка по референсам (фото товара, кадр героя) и промпту."""
        ...

    async def generate(
        self,
        prompt: str,
        aspect_ratio: str | None = None,
        progress: Progress | None = None,
    ) -> bytes:
        """Картинка только по тексту: сцена без товара, герой появляется впервые."""
        ...


class VideoGenProvider(Protocol):
    @property
    def can_continue(self) -> bool:
        """Есть ли модель для продолжения по хвосту видео."""
        ...

    async def image_to_video(
        self,
        image: ImageInput,
        prompt: str,
        duration: int,
        end_image: ImageInput | None = None,
        seed: int | None = None,
        progress: Progress | None = None,
    ) -> bytes:
        """MP4-клип: стартовый кадр + промпт движения. duration — из durations модели."""
        ...

    async def continue_video(
        self,
        tail: bytes,
        images: list[ImageInput],
        prompt: str,
        duration: int,
        seed: int | None = None,
        progress: Progress | None = None,
    ) -> bytes:
        """Бесшовное продолжение: хвост прошлого клипа как референс движения.

        Стоп-кадр продолжает картинку, но камера остаётся стоять на месте. Кусок видео
        даёт модели ещё и движение — стык перестаёт читаться. Дороже, поэтому вызывается
        только там, где оператор пометил стык критичным.
        """
        ...
