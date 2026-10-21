"""ElevenLabs: озвучка + посимвольные тайминги (для субтитров и подгонки клипов).

Перенесено из AI_Host (render/eleven.py). Стек ударений прототипа не переносим.

ЗАЩИТА ОТ ОБРЫВА. ElevenLabs изредка возвращает НЕПОЛНУЮ озвучку: валидный mp3,
просто короче, без ошибки. Ловится надёжно: склеиваем символы из разметки и сравниваем
с отправленным текстом — в исправном прогоне совпадает символ в символ. Не совпало —
переозвучиваем.
"""

from __future__ import annotations

import base64
import logging

from .. import costs, net, pace
from ..config import settings
from ..render import ffmpeg
from .base import Speech, words_from_chars

log = logging.getLogger("adreel.tts.elevenlabs")

BASE = "https://api.elevenlabs.io"
MAX_ATTEMPTS = 3
#: доля текста, ниже которой ответ считаем обрезанным (допуск на нормализацию пробелов)
MIN_COVERAGE = 0.98


class TTSError(RuntimeError):
    pass


class ElevenLabsTTS:
    def __init__(self, *, api_key: str, voice_id: str, model: str,
                 stability: float, similarity: float, tempo: float) -> None:
        if not (api_key and voice_id and model):
            raise TTSError(
                "нужны ELEVENLABS_API_KEY, ELEVENLABS_VOICE_ID и ELEVENLABS_MODEL (.env)"
            )
        self.api_key = api_key
        self.voice_id = voice_id
        self.model = model
        self.stability = stability
        self.similarity = similarity
        self.tempo = tempo

    async def _call(self, text: str) -> tuple[bytes, list[str], list[float], list[float]]:
        # retry_429=False: у ElevenLabs 429 — исчерпанная квота, повтор её не вылечит.
        r = await net.request(
            "POST", f"{BASE}/v1/text-to-speech/{self.voice_id}/with-timestamps",
            who="ElevenLabs", timeout=180, retry_429=False,
            headers={"xi-api-key": self.api_key, "Content-Type": "application/json"},
            json={
                "text": text,
                "model_id": self.model,
                # Без явных настроек модель читает каждый раз по-разному: замерено
                # в AI_Host, stability 1.0 режет разброс длительности вдвое.
                "voice_settings": {"stability": self.stability,
                                   "similarity_boost": self.similarity},
            },
        )
        if r.status_code in (401, 429):
            raise TTSError(f"ElevenLabs {r.status_code}: неверный ключ или кончилась квота. "
                           f"{r.text[:200]}")
        r.raise_for_status()
        j = r.json()
        al = j.get("alignment") or {}
        return (base64.b64decode(j["audio_base64"]), al.get("characters", []),
                al.get("character_start_times_seconds", []),
                al.get("character_end_times_seconds", []))

    async def synthesize(self, text: str) -> Speech:
        best: tuple[float, bytes, list[str], list[float], list[float]] | None = None
        for attempt in range(MAX_ATTEMPTS):
            audio, chars, starts, ends = await self._call(text)
            costs.record("elevenlabs", self.model, len(text), "chars", costs.tts_usd(len(text)))
            cov = len("".join(chars)) / len(text) if text else 1.0
            if cov >= MIN_COVERAGE:
                speech = await self._finish(audio, chars, starts, ends)
                pace.observe(len(text), speech.duration)
                return speech
            if best is None or cov > best[0]:
                best = (cov, audio, chars, starts, ends)
            log.warning("озвучка оборвалась на %.0f%% текста — попытка %d из %d",
                        cov * 100, attempt + 2, MAX_ATTEMPTS)
        assert best is not None
        # Обрезанный текст в ролик пускать нельзя — пропавшее слово в рекламе хуже паузы.
        raise TTSError(f"после {MAX_ATTEMPTS} попыток озвучено лишь {best[0]:.0%} текста")

    async def _finish(self, audio: bytes, chars: list[str],
                      starts: list[float], ends: list[float]) -> Speech:
        """Ускорение + сжатие таймингов тем же множителем, чтобы субтитры не разъехались."""
        t = self.tempo
        if abs(t - 1.0) >= 0.01:
            audio = await ffmpeg.atempo_mp3(audio, t)
            starts = [s / t for s in starts]
            ends = [e / t for e in ends]
        words = words_from_chars(chars, starts, ends)
        return Speech(audio=audio, words=words, duration=ends[-1] if ends else 0.0)


def make_tts() -> ElevenLabsTTS:
    return ElevenLabsTTS(
        api_key=settings.elevenlabs_api_key, voice_id=settings.elevenlabs_voice_id,
        model=settings.elevenlabs_model, stability=settings.elevenlabs_stability,
        similarity=settings.elevenlabs_similarity, tempo=settings.tts_tempo,
    )
