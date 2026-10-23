"""Монтаж ролика: клипы + озвучка + субтитры + музыка → один готовый MP4.

Правила, которые держим (уроки из AI_Host и из того, как клеятся AI-клипы):
  • звук главный: клип режется под длительность сцены из сценария;
  • у продолжающих сцен выбрасываем первые кадры — на стыке генерации бывает артефакт;
  • музыка тише голоса и приседает под ним (sidechaincompress);
  • громкость в конце приводится к -14 LUFS, как просят площадки.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from ..script.schema import VideoScript, Voiceover
from ..tts.base import Speech, TTSProvider, Word
from . import ffmpeg
from .subtitles import Caption, SubtitleStyle, build_ass

log = logging.getLogger("adreel.render.montage")

WIDTH, HEIGHT, FPS = 1080, 1920, 30
#: первые кадры продолжающей сцены бывают битые — выбрасываем
CHAIN_TRIM = 0.08
#: запас кадров в хвосте сегмента: клип бывает чуть короче сцены (срез на стыке,
#: модель отдала 4.96 с вместо 5) — добиваем стоп-кадром, лишнее срежет -t
PAD_SEC = 0.5
#: музыка под голосом
MUSIC_GAIN_DB = -14.0
TARGET_LUFS = -14


@dataclass
class ShotClip:
    n: int
    path: Path
    duration: float          # сколько секунд этой сцены идёт в ролик
    chained: bool = False    # сцена продолжает предыдущую
    on_screen_text: str = ""
    #: брать КОНЕЦ клипа, а не начало: там переход или с последнего кадра стартует
    #: следующая сцена (ShotPlans.needs_tail)
    tail: bool = False


@dataclass
class MusicPart:
    path: Path
    start: float
    length: float
    loop: bool = False
    #: несколько кусков трека подряд, секунды трека; пусто — один кусок start/length
    parts: list[tuple[float, float]] = field(default_factory=list)


@dataclass
class Montage:
    video: bytes
    duration: float
    subtitles: str = ""
    lines: list[Word] = field(default_factory=list)


async def build(clips: list[ShotClip], script: VideoScript, voiceover: Voiceover | None,
                music: MusicPart | None, tts: TTSProvider | None, workdir: Path,
                style: SubtitleStyle | None = None, fonts_dir: Path | None = None) -> Montage:
    workdir.mkdir(parents=True, exist_ok=True)
    style = style or SubtitleStyle()
    _put_fonts(fonts_dir, workdir)

    segments = await _segments(clips, workdir)
    silent = workdir / "video.mp4"
    await ffmpeg.concat(segments, silent)

    words, voice_path = await _voice_track(clips, voiceover, tts, workdir)
    total = sum(c.duration for c in clips)
    audio = await _audio_track(voice_path, music, total, workdir)

    captions = scene_captions(clips)
    ass = build_ass(words, WIDTH, HEIGHT, style, captions) if words or captions else ""
    subs = workdir / "subs.ass"
    if ass:
        subs.write_text(ass, encoding="utf-8")

    out = workdir / "final.mp4"
    await _mux(silent, audio, subs if ass else None, out)
    return Montage(out.read_bytes(), total, ass, words)


def _put_fonts(fonts_dir: Path | None, workdir: Path) -> None:
    """Шрифт кладём рядом с .ass и подключаем относительным путём.

    Урок из AI_Host: абсолютный путь на Windows содержит «C:», а двоеточие в фильтре
    ffmpeg — разделитель параметров. Плюс на сервере нужного шрифта может не быть
    в системе, и libass молча возьмёт запасной.
    """
    if fonts_dir is None or not fonts_dir.exists():
        return
    for font in fonts_dir.glob("*.ttf"):
        target = workdir / font.name
        if not target.exists():
            target.write_bytes(font.read_bytes())


def scene_captions(clips: list[ShotClip]) -> list[Caption]:
    """Надпись сцены (on_screen_text) — на всё время сцены в ролике."""
    out: list[Caption] = []
    at = 0.0
    for clip in clips:
        if clip.on_screen_text.strip():
            out.append(Caption(round(at, 3), round(at + clip.duration, 3), clip.on_screen_text))
        at += clip.duration
    return out


async def _segments(clips: list[ShotClip], workdir: Path) -> list[Path]:
    """Каждая сцена — отдельный сегмент одинакового формата: иначе склейка развалится.

    Длина сегмента — ровно длительность сцены. У продолжающей сцены срезаем начало
    (CHAIN_TRIM), и если клип ровно длиной в сцену (у H3 целые 5–15 с), сегмент вышел бы
    короче — голос и субтитры уезжали бы на 0.08 с на каждом таком стыке. Поэтому хвост
    добиваем повтором последнего кадра (tpad), а длину режем на выходе (-t после -i).
    """
    out: list[Path] = []
    for clip in clips:
        dst = workdir / f"seg{clip.n:02d}.mp4"
        start = CHAIN_TRIM if clip.chained else 0.0
        if clip.tail:
            # Клип длиннее сцены, а нужен его конец: там смаз, шторка или кадр, с которого
            # стартует следующая сцена. Начало клипа отрезало бы их (урок 23.10.2026)
            length = (await ffmpeg.probe(clip.path)).duration
            start = max(start, length - clip.duration)
        vf = (f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,"
              f"crop={WIDTH}:{HEIGHT},fps={FPS},setsar=1,"
              f"tpad=stop_mode=clone:stop_duration={PAD_SEC}")
        await ffmpeg.run(["-ss", f"{start:.3f}", "-i", str(clip.path),
                          "-t", f"{clip.duration:.3f}", "-an", "-vf", vf, "-c:v", "libx264",
                          "-pix_fmt", "yuv420p", "-preset", "veryfast", str(dst)])
        out.append(dst)
    return out


async def _voice_track(clips: list[ShotClip], voiceover: Voiceover | None,
                       tts: TTSProvider | None, workdir: Path) -> tuple[list[Word], Path | None]:
    """Озвучка по сценам: каждая строка ставится в начало своей сцены.

    Тайминги слов сдвигаются на начало сцены — из них потом строятся субтитры.
    """
    if voiceover is None or tts is None:
        return [], None
    lines = {line.n: line for line in voiceover.lines}
    words: list[Word] = []
    parts: list[tuple[float, Path]] = []
    at = 0.0
    for clip in clips:
        line = lines.get(clip.n)
        text = (line.text_tts or line.text).strip() if line else ""
        if text:
            speech: Speech = await tts.synthesize(text)
            path = workdir / f"voice{clip.n:02d}.mp3"
            path.write_bytes(speech.audio)
            parts.append((at, path))
            words += [Word(w.text, round(at + w.start, 3), round(at + w.end, 3))
                      for w in speech.words]
            if speech.duration > clip.duration + 0.35:
                log.warning("сцена %s: озвучка %.1f с длиннее сцены %.1f с",
                            clip.n, speech.duration, clip.duration)
        at = round(at + clip.duration, 3)
    if not parts:
        return [], None

    voice = workdir / "voice.mp3"
    inputs: list[str] = []
    filters = []
    for i, (offset, path) in enumerate(parts):
        inputs += ["-i", str(path)]
        filters.append(f"[{i}:a]adelay={int(offset * 1000)}|{int(offset * 1000)}[v{i}]")
    mix = "".join(f"[v{i}]" for i in range(len(parts)))
    await ffmpeg.run([*inputs, "-filter_complex",
                      ";".join(filters) + f";{mix}amix=inputs={len(parts)}:normalize=0[a]",
                      "-map", "[a]", "-c:a", "libmp3lame", "-b:a", "192k", str(voice)])
    return words, voice


async def _audio_track(voice: Path | None, music: MusicPart | None, total: float,
                       workdir: Path) -> Path | None:
    """Голос + музыка: музыка тише и приседает под голосом, итог нормализуется."""
    if voice is None and music is None:
        return None
    out = workdir / "audio.m4a"

    # apad: озвучка кончается раньше картинки, и без тишины в хвосте -shortest
    # обрезал бы готовый ролик по последнему слову
    if music is None:
        await ffmpeg.run(["-i", str(voice), "-t", f"{total:.3f}", "-af",
                          f"apad,loudnorm=I={TARGET_LUFS}:TP=-1", "-c:a", "aac", "-b:a", "192k",
                          str(out)])
        return out

    music_part = workdir / "music.mp3"
    if len(music.parts) > 1:
        cut = await ffmpeg.audio_parts(music.path, music.parts)
    else:
        cut = await ffmpeg.audio_cut(music.path, music.start, total, loop=music.loop)
    music_part.write_bytes(cut)

    if voice is None:
        # apad + -t: звук ровно по длине ролика, даже если кусок трека вышел короче
        await ffmpeg.run(["-i", str(music_part), "-t", f"{total:.3f}", "-af",
                          f"apad,loudnorm=I={TARGET_LUFS}:TP=-1",
                          "-c:a", "aac", "-b:a", "192k", str(out)])
        return out

    # sidechaincompress: музыка приседает ровно там, где говорит диктор
    chain = (f"[1:a]volume={MUSIC_GAIN_DB}dB[m];"
             "[0:a]apad[vp];[vp]asplit=2[v1][v2];"
             "[m][v2]sidechaincompress=threshold=0.03:ratio=8:attack=20:release=400[mc];"
             f"[v1][mc]amix=inputs=2:normalize=0:duration=first[mixed];"
             f"[mixed]loudnorm=I={TARGET_LUFS}:TP=-1[a]")
    await ffmpeg.run(["-i", str(voice), "-i", str(music_part), "-filter_complex", chain,
                      "-map", "[a]", "-t", f"{total:.3f}", "-c:a", "aac", "-b:a", "192k",
                      str(out)])
    return out


async def _mux(video: Path, audio: Path | None, subs: Path | None, out: Path) -> None:
    args = ["-i", str(video)]
    if audio is not None:
        args += ["-i", str(audio)]
    if subs is not None:
        # относительный путь: в фильтре ffmpeg двоеточие из «C:\» — разделитель параметров
        args += ["-vf", f"subtitles={subs.name}:fontsdir=.", "-c:v", "libx264",
                 "-pix_fmt", "yuv420p",
                 "-preset", "veryfast", "-crf", "20"]
    else:
        args += ["-c:v", "copy"]
    if audio is not None:
        args += ["-c:a", "aac", "-b:a", "192k", "-map", "0:v:0", "-map", "1:a:0", "-shortest"]
    args += ["-movflags", "+faststart", out.name]
    await ffmpeg.run(args, cwd=str(out.parent))
