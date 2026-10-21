"""Кусок трека словами владельца: «0:42–1:02», «с 30-й секунды 20 секунд», «припев», «весь».

Парсер детерминированный: время и ключевые слова. «Припев» честно определить без
отдельной модели нельзя, поэтому берём самое яркое место — кусок с дропом или
с максимальной энергией. Об этом говорим владельцу в пояснении.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import numpy as np

from .analyze import ENERGY_STEP, MusicError, MusicMap, Window

MIN_LEN = 5
MAX_LEN = 30
DEFAULT_LEN = 20
#: насколько далеко можно сдвинуть начало к сильной доле, чтобы звук вошёл в такт
SNAP_SEC = 0.35

_TIME = re.compile(r"(\d{1,2}):(\d{2})(?:[.,](\d+))?|(\d+(?:[.,]\d+)?)")
_LEN = re.compile(r"(?:на|длин\w*|по)\s*(\d+(?:[.,]\d+)?)\s*(?:с\b|сек|секунд)", re.IGNORECASE)


@dataclass
class Segment:
    window: Window
    note: str


def _seconds(m: re.Match[str]) -> float:
    if m.group(1) is not None:
        frac = float("0." + m.group(3)) if m.group(3) else 0.0
        return int(m.group(1)) * 60 + int(m.group(2)) + frac
    return float(m.group(4).replace(",", "."))


def _snap(t: float, mmap: MusicMap) -> float:
    if not mmap.downbeats:
        return t
    nearest = min(mmap.downbeats, key=lambda b: abs(b - t))
    return nearest if abs(nearest - t) <= SNAP_SEC else t


def drop_in(start: float, end: float, mmap: MusicMap) -> float | None:
    for d in mmap.drops:
        if start + 1.0 <= d <= end - 1.0:
            return round(d - start, 2)
    return None


def make(start: float, length: float, mmap: MusicMap, note: str) -> Segment:
    notes = [note] if note else []
    if length > MAX_LEN:
        notes.append(f"ролик до {MAX_LEN} с — взял первые {MAX_LEN} с")
        length = MAX_LEN
    if length < MIN_LEN:
        notes.append(f"слишком коротко — растянул до {MIN_LEN} с")
        length = MIN_LEN
    start = max(0.0, min(start, mmap.duration))
    loop = False
    if start + length > mmap.duration:
        if mmap.duration >= length:
            start = mmap.duration - length
            notes.append("кусок упёрся в конец трека — сдвинул начало")
        else:
            start, loop = 0.0, True
            notes.append("трек короче ролика — пущу по кругу")
    end = start + length
    w = Window(start=round(start, 3), end=round(end, 3), loop=loop,
               drop_at=drop_in(start, end, mmap))
    return Segment(w, "; ".join(notes))


def _loudest(length: float, mmap: MusicMap) -> float:
    e = np.asarray(mmap.energy)
    k = max(1, int(length / ENERGY_STEP))
    if len(e) <= k:
        return 0.0
    sums = np.convolve(e, np.ones(k), mode="valid")
    return float(np.argmax(sums)) * ENERGY_STEP


def parse(text: str, mmap: MusicMap, default_len: int = DEFAULT_LEN) -> Segment:
    t = text.lower().strip()
    length_m = _LEN.search(t)
    explicit_len = float(length_m.group(1).replace(",", ".")) if length_m else None
    # длину («на 20 секунд») вырезаем, чтобы её число не приняли за время начала
    t_wo_len = t[:length_m.start()] + t[length_m.end():] if length_m else t

    if re.search(r"\bвесь\b|\bвсю\b|целиком|полностью|весь трек", t):
        if mmap.duration > MAX_LEN and not explicit_len:
            return make(0.0, MAX_LEN, mmap,
                         f"трек {mmap.duration:.0f} с, а ролик до {MAX_LEN} с — взял первые "
                         f"{MAX_LEN} с. Нужен другой кусок — напиши секунды")
        return make(0.0, min(mmap.duration, explicit_len or MAX_LEN), mmap, "")

    length = explicit_len or default_len
    if re.search(r"вступлен|интро|начал|с самого начала", t):
        return make(0.0, length, mmap, "")
    if re.search(r"конец|концовк|финал|в конце", t):
        return make(max(0.0, mmap.duration - length), length, mmap, "")
    if re.search(r"припев|кульминац|дроп|пик|самое ярк|самое громк|взрыв", t):
        if mmap.drops:
            start = _snap(max(0.0, mmap.drops[0] - min(4.0, length * 0.25)), mmap)
            return make(start, length, mmap, "взял самое яркое место — с дропом")
        return make(_snap(_loudest(length, mmap), mmap), length, mmap,
                     "явного дропа нет — взял самое громкое место")

    times = [_seconds(m) for m in _TIME.finditer(t_wo_len)]
    if len(times) >= 2 and times[1] > times[0]:
        return make(_snap(times[0], mmap), times[1] - times[0], mmap, "")
    if times:
        return make(_snap(times[0], mmap), length, mmap, "")
    raise MusicError("не понял, какой кусок взять. Примеры: «0:42–1:02», "
                     "«с 30-й секунды на 20 секунд», «припев», «вступление», «весь трек»")
