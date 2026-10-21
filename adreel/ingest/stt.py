"""Расшифровка голосовых: ElevenLabs Scribe (тот же ключ, что и для озвучки)."""

from __future__ import annotations

from typing import Protocol

from .. import costs, net
from ..config import load_yaml, settings

URL = "https://api.elevenlabs.io/v1/speech-to-text"


class STTError(RuntimeError):
    pass


class STTProvider(Protocol):
    async def transcribe(self, audio: bytes, filename: str, seconds: float,
                         language: str | None = None) -> str: ...


class ScribeSTT:
    def __init__(self, api_key: str, model: str) -> None:
        if not api_key:
            raise STTError("не задан ELEVENLABS_API_KEY (.env)")
        self.api_key = api_key
        self.model = model

    async def transcribe(self, audio: bytes, filename: str, seconds: float,
                         language: str | None = None) -> str:
        data = {"model_id": self.model, "tag_audio_events": "false"}
        if language:
            data["language_code"] = language
        r = await net.request(
            "POST", URL, who="ElevenLabs Scribe", timeout=300, retry_429=False,
            headers={"xi-api-key": self.api_key},
            data=data, files={"file": (filename, audio)},
        )
        if r.status_code in (401, 429):
            raise STTError(f"ElevenLabs {r.status_code}: неверный ключ или кончилась квота")
        if r.status_code >= 400:
            raise STTError(f"ElevenLabs Scribe: HTTP {r.status_code} {r.text[:200]}")
        per_hour = float(load_yaml("pricing.yaml")["elevenlabs"]["stt_usd_per_hour"])
        costs.record("elevenlabs", self.model, round(seconds, 1), "audio_sec",
                     seconds / 3600 * per_hour)
        return str(r.json().get("text", "")).strip()


def make_stt() -> ScribeSTT:
    return ScribeSTT(settings.elevenlabs_api_key, settings.stt_model)
