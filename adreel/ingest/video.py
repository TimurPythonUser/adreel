"""Видео: скачивание по ссылке (yt-dlp) и кадры для зрения."""

from __future__ import annotations

import asyncio
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..render import ffmpeg

MAX_VIDEO_BYTES = 300 * 1024 * 1024


class VideoError(RuntimeError):
    pass


@dataclass(frozen=True)
class Downloaded:
    data: bytes
    title: str      # название ролика на площадке — имя трека в «Моих треках»


def _download(url: str, dest_dir: Path) -> tuple[Path, str]:
    import yt_dlp

    opts: dict[str, Any] = {
        "quiet": True, "no_warnings": True, "noplaylist": True, "noprogress": True,
        "outtmpl": str(dest_dir / "video.%(ext)s"),
        "format": "bv*[ext=mp4][height<=1080]+ba[ext=m4a]/b[ext=mp4]/b",
        "merge_output_format": "mp4",
        "max_filesize": MAX_VIDEO_BYTES,
        # Дефолтный web-клиент YouTube ловит антибот; android обычно проходит (AI_Host).
        "extractor_args": {"youtube": {"player_client": ["android", "tv", "web"]}},
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True) or {}
    found = sorted(dest_dir.glob("video.*"), key=lambda p: p.stat().st_size, reverse=True)
    if not found:
        raise VideoError("yt-dlp ничего не скачал")
    return found[0], str(info.get("title") or "")


async def download(url: str) -> Downloaded:
    with tempfile.TemporaryDirectory() as tmp:
        try:
            path, title = await asyncio.to_thread(_download, url, Path(tmp))
        except VideoError:
            raise
        except Exception as exc:
            raise VideoError(f"не смог скачать видео: {str(exc)[:200]}") from exc
        return Downloaded(path.read_bytes(), title)


async def download_url(url: str) -> bytes:
    return (await download(url)).data


async def frames(video: bytes, count: int = 3) -> list[bytes]:
    """Несколько кадров равномерно по длительности (JPEG) — зрению этого хватает."""
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "src.mp4"
        src.write_bytes(video)
        info = await ffmpeg.probe(src)
        out: list[bytes] = []
        for i in range(count):
            t = info.duration * (i + 1) / (count + 1)
            dst = Path(tmp) / f"f{i}.jpg"
            await ffmpeg.run(["-ss", f"{t:.2f}", "-i", str(src), "-frames:v", "1",
                              "-q:v", "3", str(dst)])
            if dst.exists():
                out.append(dst.read_bytes())
        return out
