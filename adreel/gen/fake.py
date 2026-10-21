"""Подделки генераторов для тестов: ни одного платного вызова."""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass, field

from .. import costs
from ..render import ffmpeg
from .base import ImageInput, Progress


def solid_png(width: int, height: int, rgb: tuple[int, int, int] = (40, 120, 200)) -> bytes:
    """Однотонный PNG без внешних библиотек."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    row = b"\x00" + bytes(rgb) * width
    raw = zlib.compress(row * height)
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", raw) + chunk(b"IEND", b"")


@dataclass
class FakeImageGen:
    width: int = 1080
    height: int = 1920
    calls: list[str] = field(default_factory=list)
    #: как у настоящего: каждый вызов пишет стоимость — проверяем учёт затрат
    usd_per_image: float = 0.0

    async def edit(
        self,
        images: list[ImageInput],
        prompt: str,
        aspect_ratio: str | None = None,
        progress: Progress | None = None,
    ) -> bytes:
        self.calls.append(prompt)
        costs.record("fake", "image", 1, "image", self.usd_per_image)
        return solid_png(self.width, self.height)


@dataclass
class FakeVideoGen:
    """Настоящий MP4 из тестового сигнала ffmpeg — чтобы монтаж проверялся по-честному."""

    width: int = 720
    height: int = 1280
    fps: int = 30
    can_continue: bool = True
    calls: list[tuple[str, int]] = field(default_factory=list)
    #: продолжения по хвосту видео: (промпт, длительность, сколько байт хвоста отдали)
    continues: list[tuple[str, int, int]] = field(default_factory=list)
    seeds: list[int | None] = field(default_factory=list)
    usd_per_sec: float = 0.0

    async def image_to_video(
        self,
        image: ImageInput,
        prompt: str,
        duration: int,
        end_image: ImageInput | None = None,
        seed: int | None = None,
        progress: Progress | None = None,
    ) -> bytes:
        self.calls.append((prompt, duration))
        self.seeds.append(seed)
        costs.record("fake", "video", duration, "video_sec", duration * self.usd_per_sec)
        return await ffmpeg.test_clip(duration, self.width, self.height, self.fps)

    async def continue_video(
        self,
        tail: bytes,
        images: list[ImageInput],
        prompt: str,
        duration: int,
        seed: int | None = None,
        progress: Progress | None = None,
    ) -> bytes:
        self.calls.append((prompt, duration))
        self.continues.append((prompt, duration, len(tail)))
        self.seeds.append(seed)
        costs.record("fake", "continue", duration, "video_sec", duration * self.usd_per_sec)
        return await ffmpeg.test_clip(duration, self.width, self.height, self.fps)
