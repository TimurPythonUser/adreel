"""FFmpeg и ffprobe: запуск, проба файла, склейка. Всё блокирующее уходит в поток."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


class FFmpegError(RuntimeError):
    pass


#: самая долгая операция — финальный монтаж 30 с в 1080x1920 (секунды на обычной машине).
#: Без лимита зависший ffmpeg навсегда держал бы поток и вместе с ним генерацию.
TIMEOUT_SEC = 600


def available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _run(args: list[str], *, cwd: str | None = None, stdin: bytes | None = None) -> bytes:
    try:
        res = subprocess.run(
            args, cwd=cwd, input=stdin, capture_output=True, check=False, timeout=TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired as exc:
        # subprocess.run сам убивает процесс по таймауту — остаётся сказать, кто завис
        raise FFmpegError(f"{Path(args[0]).name} завис дольше {TIMEOUT_SEC} с") from exc
    if res.returncode != 0:
        err = res.stderr.decode("utf-8", "replace")[-800:]
        raise FFmpegError(f"{Path(args[0]).name} упал (код {res.returncode}): {err}")
    return res.stdout


async def run(args: list[str], *, cwd: str | None = None, stdin: bytes | None = None) -> bytes:
    """ffmpeg с общими флагами. Возвращает stdout (для pipe:1)."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args]
    return await asyncio.to_thread(_run, cmd, cwd=cwd, stdin=stdin)


@dataclass(frozen=True)
class Probe:
    width: int
    height: int
    fps: float
    duration: float
    has_audio: bool


def _rotation(stream: dict) -> float:
    for sd in stream.get("side_data_list", []):
        if "rotation" in sd:
            return float(sd["rotation"])
    try:
        return float(stream.get("tags", {}).get("rotate", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


async def probe(path: str | Path) -> Probe:
    """Размер КАК ВИДНО НА ЭКРАНЕ (с учётом поворота), fps, длительность, наличие звука.

    Поворот важен: телефон пишет вертикальное видео как 1920x1080 плюс метку 90°
    (урок из AI_Host, compose.probe_display_size).
    """
    out = await asyncio.to_thread(
        _run,
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
    )
    data = json.loads(out)
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise FFmpegError(f"{path}: нет видеодорожки")
    w, h = int(video["width"]), int(video["height"])
    if int(abs(_rotation(video))) % 180 == 90:
        w, h = h, w
    num, _, den = str(video.get("avg_frame_rate", "0/1")).partition("/")
    fps = float(num) / float(den or 1) if float(den or 1) else 0.0
    duration = float(data.get("format", {}).get("duration") or video.get("duration") or 0)
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    return Probe(w, h, round(fps, 3), duration, has_audio)


async def probe_bytes(data: bytes) -> Probe:
    """То же, что probe, но для клипа в памяти (проверка того, что вернула модель)."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / 'clip.mp4'
        path.write_bytes(data)
        return await probe(path)


async def concat(paths: list[Path], out: Path) -> None:
    """Склейка сегментов с одинаковыми параметрами без перекодирования."""
    listing = out.with_suffix(".concat.txt")
    listing.write_text(
        "".join(f"file '{p.resolve().as_posix()}'\n" for p in paths), encoding="utf-8"
    )
    await run(["-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(out)])


async def test_clip(duration: float, width: int, height: int, fps: int = 30) -> bytes:
    """Тестовый MP4 без звука (для FakeVideoGen и тестов монтажа)."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "clip.mp4"
        await run([
            "-f", "lavfi", "-i", f"testsrc2=size={width}x{height}:rate={fps}",
            "-t", f"{duration:.3f}", "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-preset", "ultrafast", str(out),
        ])
        return out.read_bytes()


async def tone_mp3(duration: float, freq: int = 440) -> bytes:
    """Тестовый mp3 (для FakeTTS)."""
    return await run([
        "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={duration:.3f}",
        "-c:a", "libmp3lame", "-b:a", "128k", "-ar", "44100", "-f", "mp3", "pipe:1",
    ])


async def tail_frames(clip: bytes, count: int = 1) -> list[bytes]:
    """Последние кадры клипа (JPEG). Нужны, чтобы следующий клип продолжил этот без склейки.

    Берём не самый последний кадр, а чуть раньше: на хвосте генерации иногда
    появляется смаз или подёргивание.
    """
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "clip.mp4"
        src.write_bytes(clip)
        out: list[bytes] = []
        for i in range(count):
            dst = Path(tmp) / f"tail{i}.jpg"
            await run(["-sseof", f"-{0.15 + i * 0.1:.2f}", "-i", str(src), "-frames:v", "1",
                       "-q:v", "2", str(dst)])
            if dst.exists():
                out.append(dst.read_bytes())
        return out


async def cut_clip(clip: bytes, start: float, duration: float) -> bytes:
    """Кусок клипа как отдельный MP4: одна генерация может держать несколько сцен.

    Перекодируем, а не режем по ключевым кадрам: кусок должен начинаться ровно там,
    где по сценарию начинается сцена, иначе уедет озвучка.
    """
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / 'clip.mp4'
        src.write_bytes(clip)
        dst = Path(tmp) / 'piece.mp4'
        await run(['-ss', f'{start:.3f}', '-t', f'{duration:.3f}', '-i', str(src), '-an',
                   '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-preset', 'veryfast', str(dst)])
        return dst.read_bytes()


async def tail_video(clip: bytes, seconds: float) -> bytes:
    """Хвост клипа отдельным MP4 — референс движения для бесшовного продолжения.

    Модель, которой дали только стоп-кадр, продолжает картинку, но не движение:
    камера встаёт на месте. Кусок видео на входе решает это.
    """
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / 'clip.mp4'
        src.write_bytes(clip)
        dst = Path(tmp) / 'tail.mp4'
        await run(['-sseof', f'-{seconds:.2f}', '-i', str(src), '-an', '-c:v', 'libx264',
                   '-pix_fmt', 'yuv420p', '-preset', 'veryfast', str(dst)])
        return dst.read_bytes()


async def to_mp3(data: bytes, bitrate: str = "192k") -> bytes:
    """Звук из любого аудио или видео -> mp3 (например, дорожка YouTube-ролика)."""
    return await run(["-i", "pipe:0", "-vn", "-c:a", "libmp3lame", "-b:a", bitrate,
                      "-ar", "44100", "-f", "mp3", "pipe:1"], stdin=data)


async def audio_cut(src: Path, start: float, length: float, *, loop: bool = False,
                    fade_in: float = 0.3, fade_out: float = 1.0) -> bytes:
    """Кусок трека с фейдами -> mp3. loop — трек короче куска, крутим по кругу."""
    pre = ["-stream_loop", "-1"] if loop else ["-ss", f"{start:.3f}"]
    fade = (f"afade=t=in:st=0:d={fade_in:.2f},"
            f"afade=t=out:st={max(0.0, length - fade_out):.2f}:d={fade_out:.2f}")
    return await run([*pre, "-i", str(src), "-t", f"{length:.3f}", "-af", fade,
                      "-c:a", "libmp3lame", "-b:a", "192k", "-ar", "44100", "-f", "mp3",
                      "pipe:1"])


async def audio_duration(data: bytes) -> float:
    """Длительность звука декодированием.

    ffprobe на mp3 из pipe отдаёт duration=N/A (длину он берёт из заголовков файла,
    а у потока их нет), поэтому просто считаем сэмплы.
    """
    rate = 8000
    pcm = await run(["-i", "pipe:0", "-vn", "-ac", "1", "-ar", str(rate), "-f", "s16le",
                     "pipe:1"], stdin=data)
    return len(pcm) / 2 / rate


async def atempo_mp3(mp3: bytes, tempo: float) -> bytes:
    """Ускорить речь без изменения высоты голоса.

    Битрейт задан явно: по умолчанию ffmpeg жмёт mp3 в 64 kbps, а ElevenLabs отдаёт
    128 — без этого ускорение роняло качество звука вдвое (урок из AI_Host).
    """
    return await run([
        "-i", "pipe:0", "-filter:a", f"atempo={tempo:.4f}",
        "-c:a", "libmp3lame", "-b:a", "128k", "-ar", "44100", "-f", "mp3", "pipe:1",
    ], stdin=mp3)
