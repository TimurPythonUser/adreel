"""Файлы материалов на диске: адресация по sha256, одинаковый файл хранится один раз."""

from __future__ import annotations

import asyncio
import hashlib
import io
import mimetypes
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, UnidentifiedImageError

#: форматы, которые понимают и Claude, и fal; остальное перегоняем в PNG
SAFE_IMAGE_MIME = {"image/jpeg", "image/png", "image/webp"}
MAX_IMAGE_BYTES = 15 * 1024 * 1024

#: mimetypes на Windows угадывает расширения по реестру и выдаёт экзотику (.jpe, .mp2)
_EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
        "audio/mpeg": ".mp3", "audio/mp4": ".m4a", "audio/aac": ".aac", "audio/wav": ".wav",
        "audio/x-wav": ".wav", "audio/ogg": ".ogg", "audio/flac": ".flac",
        "video/mp4": ".mp4", "video/quicktime": ".mov", "video/webm": ".webm"}


@dataclass(frozen=True)
class Stored:
    rel_path: str      # относительно workdir — переносится вместе с папкой
    sha256: str
    mime: str
    size: int


@dataclass(frozen=True)
class ImageInfo:
    data: bytes
    mime: str
    width: int
    height: int


class MediaStore:
    def __init__(self, workdir: Path) -> None:
        self.workdir = workdir
        self.root = workdir / "media"

    def save(self, data: bytes, mime: str) -> Stored:
        digest = hashlib.sha256(data).hexdigest()
        ext = _EXT.get(mime) or mimetypes.guess_extension(mime) or ".bin"
        path = self.root / digest[:2] / f"{digest}{ext}"
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_bytes(data)
            tmp.replace(path)
        return Stored(path.relative_to(self.workdir).as_posix(), digest, mime, len(data))

    def path(self, rel_path: str) -> Path:
        return self.workdir / rel_path

    def read(self, rel_path: str) -> bytes:
        return self.path(rel_path).read_bytes()

    # Видео до сотен МБ: синхронная запись и хеширование прямо в event loop
    # подвешивали бы бота на это время. В async-коде — только эти два.

    async def save_async(self, data: bytes, mime: str) -> Stored:
        return await asyncio.to_thread(self.save, data, mime)

    async def read_async(self, rel_path: str) -> bytes:
        return await asyncio.to_thread(self.read, rel_path)


class NotAnImage(ValueError):
    pass


def inspect_image(data: bytes) -> ImageInfo:
    """Проверить, что это картинка (по содержимому, а не расширению), и привести формат.

    AVIF, HEIC, GIF и прочее перегоняем в PNG: моделям нужен предсказуемый формат.
    """
    if len(data) > MAX_IMAGE_BYTES:
        raise NotAnImage(f"картинка больше {MAX_IMAGE_BYTES // 1024 // 1024} МБ")
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.load()
            mime = Image.MIME.get(im.format or "", "")
            w, h = im.size
            if mime not in SAFE_IMAGE_MIME:
                buf = io.BytesIO()
                im.convert("RGBA" if "A" in im.getbands() else "RGB").save(buf, "PNG")
                data, mime = buf.getvalue(), "image/png"
    except (UnidentifiedImageError, OSError) as exc:
        raise NotAnImage("это не картинка") from exc
    return ImageInfo(data, mime, w, h)
