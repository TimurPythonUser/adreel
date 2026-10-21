"""Карта музыки: темп, биты, энергия, дропы и лучшие куски под длину ролика.

Зачем. Сценарист режет ролик на кадры и должен знать, где у трека сильные доли
и где «взрыв»: хук ставим на вступление, раскрытие товара — на дроп, склейки — на биты.
Разбор делается один раз при загрузке трека и хранится в БД.

Звук декодируем через ffmpeg (а не librosa.load): mp3, m4a, ogg из Telegram и видео
открываются одинаково, без зависимости от кодеков soundfile.
"""

from __future__ import annotations

import asyncio
import subprocess

import librosa
import numpy as np
from pydantic import BaseModel, Field

SR = 22050
#: шаг кривой энергии, секунд
ENERGY_STEP = 0.5
#: длины роликов, под которые заранее ищем куски трека
TARGET_LENGTHS = (15, 20, 30)
#: сколько вариантов куска держим на каждую длину («Другой кусок»)
WINDOWS_PER_LENGTH = 3
MIN_TRACK_SEC = 8.0
MAX_TRACK_SEC = 600.0
#: декодирование 10-минутного трека — секунды; минута — уже точно зависание
DECODE_TIMEOUT_SEC = 120


class MusicError(RuntimeError):
    pass


class Window(BaseModel):
    start: float
    end: float
    #: момент дропа внутри куска, секунды от начала КУСКА (None — дропа нет)
    drop_at: float | None = None
    score: float = 0.0
    #: трек короче ролика — кусок придётся зацикливать
    loop: bool = False


class MusicMap(BaseModel):
    duration: float
    bpm: float
    beats: list[float] = Field(description="время битов, с")
    downbeats: list[float] = Field(description="сильные доли (начало такта), с")
    energy: list[float] = Field(description=f"громкость 0..1 с шагом {ENERGY_STEP} с")
    drops: list[float] = Field(default_factory=list, description="резкие нарастания, с")
    windows: dict[str, list[Window]] = Field(default_factory=dict)

    def best(self, seconds: int, index: int = 0) -> Window:
        options = self.windows.get(str(seconds)) or []
        if not options:
            raise MusicError(f"для ролика {seconds} с кусков трека нет")
        return options[index % len(options)]

    def describe(self, seconds: int) -> str:
        """Строка для промпта сценариста (этап 3)."""
        w = self.best(seconds)
        rel_beats = [round(b - w.start, 2) for b in self.downbeats if w.start <= b < w.end]
        drop = f"дроп на {w.drop_at:.1f} с" if w.drop_at is not None else "без явного дропа"
        return (f"Темп {self.bpm:.0f} BPM, кусок {w.start:.1f}–{w.end:.1f} с, {drop}. "
                f"Сильные доли от начала ролика: {rel_beats[:24]}")


def decode(data: bytes) -> np.ndarray:
    """Любой аудио/видеофайл -> моно float32 22050 Гц."""
    try:
        res = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0", "-vn",
             "-ac", "1", "-ar", str(SR), "-f", "f32le", "pipe:1"],
            input=data, capture_output=True, check=False, timeout=DECODE_TIMEOUT_SEC)
    except subprocess.TimeoutExpired as exc:
        raise MusicError("трек не читается — ffmpeg завис. Пришли mp3") from exc
    if res.returncode != 0 or not res.stdout:
        raise MusicError("не смог прочитать звук — пришли mp3, m4a или wav")
    return np.frombuffer(res.stdout, dtype=np.float32)


def analyze(y: np.ndarray, sr: int = SR) -> MusicMap:
    duration = len(y) / sr
    if duration < MIN_TRACK_SEC:
        raise MusicError(f"трек короче {MIN_TRACK_SEC:.0f} с — для ролика мало")
    if duration > MAX_TRACK_SEC:
        raise MusicError("трек длиннее 10 минут — пришли покороче")

    onset = librosa.onset.onset_strength(y=y, sr=sr)
    tempo, beat_frames = librosa.beat.beat_track(onset_envelope=onset, sr=sr)
    beats = librosa.frames_to_time(beat_frames, sr=sr).tolist()
    # Темп — по найденным битам, а не по оценке librosa: та округлена по сетке кадров
    # и на ровных 120 BPM давала 117.5. Соседние интервалы тоже квантованы (0.488/0.511),
    # поэтому берём медиану интервалов через 4 бита: квант усредняется, а медиана
    # не боится пропущенного бита.
    if len(beats) >= 8:
        b = np.asarray(beats)
        bpm = 60.0 / float(np.median((b[4:] - b[:-4]) / 4))
    else:
        bpm = float(np.atleast_1d(tempo)[0])
    downbeats = _downbeats(beats, beat_frames, onset)

    energy = _energy_curve(y, sr)
    drops = _drops(energy)
    windows = {str(n): _windows(n, duration, downbeats or beats, energy, drops)
               for n in TARGET_LENGTHS}
    return MusicMap(duration=round(duration, 3), bpm=round(bpm, 1),
                    beats=[round(b, 3) for b in beats],
                    downbeats=[round(b, 3) for b in downbeats],
                    energy=[round(e, 3) for e in energy], drops=drops, windows=windows)


async def analyze_bytes(data: bytes) -> MusicMap:
    return await asyncio.to_thread(lambda: analyze(decode(data)))


def _downbeats(beats: list[float], frames: np.ndarray, onset: np.ndarray) -> list[float]:
    """Сильные доли: каждый 4-й бит, со сдвигом, на котором атака в среднем сильнее.

    Честное определение такта — отдельная модель; для склеек в рекламе хватает
    размера 4/4, который у поп- и рекламной музыки почти всегда.
    """
    if len(beats) < 8:
        return list(beats)
    strength = onset[np.clip(frames, 0, len(onset) - 1)]
    offset = int(np.argmax([strength[i::4].mean() for i in range(4)]))
    return beats[offset::4]


def _energy_curve(y: np.ndarray, sr: int) -> list[float]:
    hop = int(sr * ENERGY_STEP)
    rms = librosa.feature.rms(y=y, frame_length=hop * 2, hop_length=hop, center=False)[0]
    if rms.size == 0:
        return []
    top = float(np.percentile(rms, 95)) or 1.0
    return np.clip(rms / top, 0.0, 1.0).tolist()


def _drops(energy: list[float], span: float = 2.0, min_jump: float = 0.25) -> list[float]:
    """Моменты, где средняя громкость следующих 2 с резко выше предыдущих 2 с."""
    e = np.asarray(energy)
    k = int(span / ENERGY_STEP)
    if len(e) < 2 * k + 1:
        return []
    jumps = np.array([e[i:i + k].mean() - e[i - k:i].mean() for i in range(k, len(e) - k)])
    found: list[float] = []
    for i in np.argsort(jumps)[::-1]:
        if jumps[i] < min_jump:
            break
        t = (i + k) * ENERGY_STEP
        if all(abs(t - f) > 4 for f in found):
            found.append(round(t, 2))
        if len(found) >= 5:
            break
    return sorted(found)


def script_windows(mmap: MusicMap, length: float, key_at: float) -> list[Window]:
    """Куски под готовый сценарий: дроп должен попасть на начало главного кадра (key_at)."""
    return _windows(length, mmap.duration, mmap.downbeats or mmap.beats, mmap.energy,
                    mmap.drops, key_at=key_at)


def _windows(length: float, duration: float, grid: list[float], energy: list[float],
             drops: list[float], key_at: float | None = None) -> list[Window]:
    """Лучшие куски длины length. Начало — на сильной доле, чтобы звук стартовал в такт.

    Оценка: половина средней энергии + бонус, если дроп в первой половине куска
    (хук до дропа, товар на дропе) − штраф за почти тишину в начале.
    Бонус за дроп весит больше энергии намеренно: кусок «сплошь громко» звучит ровно,
    а нарастание с ударом и есть то, под что режется реклама.
    """
    if duration <= length:
        return [Window(start=0.0, end=round(duration, 3), loop=True)]
    e = np.asarray(energy)
    candidates = [t for t in grid if t + length <= duration] or [0.0]
    scored: list[Window] = []
    for start in candidates:
        a, b = int(start / ENERGY_STEP), int((start + length) / ENERGY_STEP)
        seg = e[a:b]
        if seg.size == 0:
            continue
        score = 0.5 * float(seg.mean())
        head = float(seg[: max(1, int(2 / ENERGY_STEP))].mean())
        if head < 0.08:
            score -= 0.2
        drop_at = None
        for d in drops:
            rel = d - start
            if key_at is not None:
                # под сценарий: главный кадр на дроп важнее всего остального
                if abs(rel - key_at) <= 1.0:
                    drop_at = round(rel, 2)
                    score += 0.8 - 0.3 * abs(rel - key_at)
                    break
            elif 2.0 <= rel <= length * 0.6:
                drop_at = round(rel, 2)
                score += 0.5
                break
        scored.append(Window(start=round(start, 3), end=round(start + length, 3),
                             drop_at=drop_at, score=round(score, 3)))
    scored.sort(key=lambda w: w.score, reverse=True)
    picked: list[Window] = []
    for w in scored:
        # варианты не должны почти совпадать — иначе «Другой кусок» звучит так же
        if all(abs(w.start - p.start) >= length / 2 for p in picked):
            picked.append(w)
        if len(picked) >= WINDOWS_PER_LENGTH:
            break
    return picked
