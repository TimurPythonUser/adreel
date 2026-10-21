"""Подделка озвучки: настоящий mp3 (тон) и равномерные тайминги слов."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..render import ffmpeg
from .base import Speech, Word


@dataclass
class FakeTTS:
    chars_per_sec: float = 14.5
    #: False — не звать ffmpeg (для быстрых тестов, где звук не нужен)
    with_audio: bool = True
    calls: list[str] = field(default_factory=list)

    async def synthesize(self, text: str) -> Speech:
        self.calls.append(text)
        tokens = text.split()
        duration = max(0.5, len(text) / self.chars_per_sec)
        total = sum(len(t) for t in tokens) or 1
        words, t = [], 0.0
        for tok in tokens:
            dt = duration * len(tok) / total
            words.append(Word(tok, round(t, 3), round(t + dt * 0.9, 3)))
            t += dt
        audio = await ffmpeg.tone_mp3(duration) if self.with_audio else b""
        return Speech(audio=audio, words=words, duration=duration)
