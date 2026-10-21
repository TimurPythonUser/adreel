"""Интерфейс озвучки. Реализации: elevenlabs.py (живая), fake.py (тесты)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Word:
    text: str
    start: float   # секунды от начала озвучки
    end: float


@dataclass(frozen=True)
class Speech:
    audio: bytes           # mp3
    words: list[Word]
    duration: float        # секунды


class TTSProvider(Protocol):
    async def synthesize(self, text: str) -> Speech: ...


def words_from_chars(
    chars: Sequence[str], starts: Sequence[float], ends: Sequence[float]
) -> list[Word]:
    """Пословные тайминги из посимвольных (так их отдаёт ElevenLabs /with-timestamps).

    Знаки ударения (U+0301) из экранного текста вычищаются.
    """
    words: list[Word] = []
    buf: list[str] = []
    w_start = 0.0
    w_end = 0.0
    for c, s, e in zip(chars, starts, ends, strict=False):
        if c.strip() == "":
            if buf:
                words.append(Word(_clean("".join(buf)), w_start, w_end))
                buf = []
        else:
            if not buf:
                w_start = s
            buf.append(c)
            w_end = e
    if buf:
        words.append(Word(_clean("".join(buf)), w_start, w_end))
    return [w for w in words if w.text]


def _clean(s: str) -> str:
    return s.replace("́", "").replace("´", "")
