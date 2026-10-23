"""Музыка ролика: свой трек → разбор → кусок под длину ролика → превью.

Генерации музыки нет — решение владельца: треки только свои (docs/DECISIONS.md).
"""

from __future__ import annotations

import logging
import mimetypes
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import PurePath

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..db.models import Material, Product, Project, Track
from ..ingest import pages, video
from ..ingest.media import MediaStore
from ..render import ffmpeg
from ..script.schema import VideoScript
from . import segment
from .analyze import (
    TARGET_LENGTHS,
    MusicError,
    MusicMap,
    Window,
    analyze_bytes,
    script_windows,
)

log = logging.getLogger("adreel.music")

AUDIO_EXT_MIME = {".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac",
                  ".wav": "audio/wav", ".ogg": "audio/ogg", ".oga": "audio/ogg",
                  ".flac": "audio/flac", ".opus": "audio/ogg"}

TITLE_MAX = 255   # длина tracks.title


def _clean_title(raw: str) -> str:
    """Одна строка без лишних пробелов: название идёт в подпись кнопки."""
    return " ".join(raw.split())[:TITLE_MAX]


def _nearest_length(seconds: int) -> int:
    """Подобранные куски есть для 15/20/30 с — берём ближайшую длину к текущей."""
    return min(TARGET_LENGTHS, key=lambda n: abs(n - seconds))


@dataclass
class PendingTrack:
    """Разобранный трек до сохранения в библиотеку."""

    rel_path: str
    sha256: str
    title: str
    source: str
    analysis: dict

    @property
    def map(self) -> MusicMap:
        return MusicMap.model_validate(self.analysis)


@dataclass
class Choice:
    track: Track
    window: Window            # первый (или единственный) кусок
    index: int
    duration: int
    map: MusicMap
    #: куски трека подряд; один кусок — [window]
    parts: list[Window] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.parts:
            self.parts = [self.window]

    @property
    def multi(self) -> bool:
        return len(self.parts) > 1

    @property
    def length(self) -> float:
        return round(sum(w.end - w.start for w in self.parts), 3)

    @property
    def drop_at(self) -> float | None:
        """Дроп на шкале РОЛИКА: в склейке кусков он может прийтись на любой из них."""
        at = 0.0
        for w in self.parts:
            if w.drop_at is not None:
                return round(at + w.drop_at, 2)
            at += w.end - w.start
        return None


class MusicService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        media: MediaStore,
        download_video: Callable[[str], Awaitable[video.Downloaded]] = video.download,
        analyze: Callable[[bytes], Awaitable[MusicMap]] = analyze_bytes,
    ) -> None:
        self.sessions = sessions
        self.media = media
        self._download_video = download_video
        self._analyze = analyze

    # ---------- ролик ----------

    async def start_project(self, product_id: int) -> int:
        async with self.sessions() as s:
            if await s.get(Product, product_id) is None:
                raise MusicError("товар не найден — начни с /new")
            p = Project(product_id=product_id)
            s.add(p)
            await s.commit()
            return p.id

    async def project(self, project_id: int) -> Project:
        async with self.sessions() as s:
            p = await s.get(Project, project_id)
            if p is None:
                raise MusicError("ролик не найден — начни с /new")
            return p

    # ---------- библиотека ----------

    async def tracks(self, owner_id: int, limit: int = 8) -> list[Track]:
        async with self.sessions() as s:
            rows = await s.scalars(select(Track).where(Track.owner_id == owner_id)
                                   .order_by(Track.last_used_at.desc()).limit(limit))
            return list(rows)

    async def count_tracks(self, owner_id: int) -> int:
        async with self.sessions() as s:
            return await s.scalar(select(func.count()).select_from(Track)
                                  .where(Track.owner_id == owner_id)) or 0

    async def track(self, owner_id: int, track_id: int) -> Track:
        async with self.sessions() as s:
            return await self._own_track(s, owner_id, track_id)

    async def rename_track(self, owner_id: int, track_id: int, title: str) -> Track:
        name = _clean_title(title)
        if not name:
            raise MusicError("название пустое — напиши, как назвать трек")
        async with self.sessions() as s:
            t = await self._own_track(s, owner_id, track_id)
            t.title = name
            await s.commit()
            return t

    async def delete_track(self, owner_id: int, track_id: int) -> str:
        """Убрать трек из библиотеки, вернуть его название.

        Ролики с этим треком остаются без музыки: FK сам обнулит track_id, но кусок
        (music_start/music_end) висел бы от чужого трека — чистим явно. Файл адресуется
        по sha256 и может быть общим с материалом товара — удаляем только ничейный."""
        async with self.sessions() as s:
            t = await self._own_track(s, owner_id, track_id)
            title, rel_path = t.title, t.file_path
            await s.execute(update(Project).where(Project.track_id == track_id)
                            .values(track_id=None, music_start=None, music_end=None,
                                    music_parts=None))
            await s.delete(t)
            await s.flush()
            users = sum([
                await s.scalar(select(func.count()).select_from(Track)
                               .where(Track.file_path == rel_path)) or 0,
                await s.scalar(select(func.count()).select_from(Material)
                               .where(Material.file_path == rel_path)) or 0,
            ])
            await s.commit()
        if not users:
            try:
                self.media.path(rel_path).unlink(missing_ok=True)
            except OSError:
                # На Windows открытый файл не удалить — строки в БД уже нет, файл просто лежит.
                log.warning("не удалил файл трека %s", rel_path, exc_info=True)
        return title

    @staticmethod
    async def _own_track(s: AsyncSession, owner_id: int, track_id: int) -> Track:
        t = await s.get(Track, track_id)
        if t is None or t.owner_id != owner_id:
            raise MusicError("трек не найден — может, уже удалён")
        return t

    # ---------- загрузка ----------

    async def prepare_upload(self, owner_id: int, data: bytes, filename: str,
                             title: str = "", source: str = "telegram",
                             mime: str = "") -> Track | PendingTrack:
        """Трек уже есть в библиотеке — вернём его без повторного разбора.
        Новый — разберём и вернём PendingTrack."""
        mime = mime or AUDIO_EXT_MIME.get(PurePath(filename).suffix.lower(), "") \
            or mimetypes.guess_type(filename)[0] or "audio/mpeg"
        stored = await self.media.save_async(data, mime)
        async with self.sessions() as s:
            existing = await s.scalar(select(Track).where(
                Track.owner_id == owner_id, Track.sha256 == stored.sha256))
            if existing is not None:
                return existing
        mmap = await self._analyze(data)
        name = _clean_title(title) or _clean_title(PurePath(filename).stem) or "Трек"
        return PendingTrack(stored.rel_path, stored.sha256, name, source, mmap.model_dump())

    async def prepare_link(self, owner_id: int, url: str) -> Track | PendingTrack:
        if not pages.is_video_url(url):
            raise MusicError("по ссылке беру музыку только с YouTube — остальное пришли файлом")
        try:
            clip = await self._download_video(url)
        except video.VideoError as exc:
            raise MusicError(f"{exc}. Пришли трек файлом.") from exc
        audio = await ffmpeg.to_mp3(clip.data)
        # Название ролика вместо общего «Трек с YouTube»: иначе в библиотеке ряд
        # одинаковых кнопок.
        return await self.prepare_upload(owner_id, audio, "youtube.mp3",
                                         title=clip.title or "Трек с YouTube",
                                         source=url, mime="audio/mpeg")

    async def save_track(self, owner_id: int, pending: PendingTrack) -> Track:
        m = pending.map
        async with self.sessions() as s:
            t = Track(owner_id=owner_id, title=pending.title, source=pending.source,
                      file_path=pending.rel_path, sha256=pending.sha256,
                      duration=m.duration, bpm=m.bpm, analysis=pending.analysis)
            s.add(t)
            await s.commit()
            return t

    # ---------- выбор куска ----------

    async def choose(self, project_id: int, track_id: int, index: int = 0) -> Choice:
        """Подобранный кусок №index. Есть сценарий — под него: длина ролика и дроп
        на главном кадре. Нет — под длину ролика из заранее посчитанных кусков."""
        async with self.sessions() as s:
            p, t = await self._pair(s, project_id, track_id)
            mmap = MusicMap.model_validate(t.analysis)
            script = VideoScript.model_validate(p.script) if p.script else None
            if script is not None:
                length = round(script.total)
                options = script_windows(mmap, script.total, script.start_of(script.key_shot))
            else:
                length = _nearest_length(p.duration_sec)
                options = mmap.windows.get(str(length)) or []
            if not options:
                raise MusicError(f"не нашёл кусок трека под ролик {length} с")
            index %= len(options)
            w = options[index]
            self._apply(p, t, w, length)
            p.music_window = index
            await s.commit()
            return Choice(t, w, index, length, mmap)

    async def choose_custom(self, project_id: int, track_id: int,
                            spec: str) -> tuple[Choice, str]:
        """Кусок словами владельца. Его длина становится длиной ролика.

        Можно и несколько кусков подряд: «с 10 по 30, а в конце с 70 по 80» — склеим
        их мягким переходом, длина ролика — сумма кусков."""
        async with self.sessions() as s:
            p, t = await self._pair(s, project_id, track_id)
            mmap = MusicMap.model_validate(t.analysis)
            parts = segment.parse_parts(spec, mmap)
            if parts is not None:
                if p.script:
                    parts = segment.fit_parts(parts, VideoScript.model_validate(p.script).total,
                                              mmap)
                length = round(parts.length)
                self._apply(p, t, parts.windows[0], length)
                p.music_parts = [[w.start, w.end] for w in parts.windows]
                await s.commit()
                return (Choice(t, parts.windows[0], p.music_window, length, mmap,
                               parts.windows), parts.note)
            seg = segment.parse(spec, mmap, default_len=p.duration_sec)
            if p.script:
                # Сценарий уже задал длину ролика: от владельца берём только начало куска.
                total = VideoScript.model_validate(p.script).total
                if abs((seg.window.end - seg.window.start) - total) > 0.5:
                    note = f"длина под сценарий — {total:g} с"
                    seg = segment.make(seg.window.start, total, mmap,
                                       "; ".join(x for x in (seg.note, note) if x))
            length = round(seg.window.end - seg.window.start)
            self._apply(p, t, seg.window, length)
            await s.commit()
            return Choice(t, seg.window, p.music_window, length, mmap), seg.note

    @staticmethod
    async def _pair(s: AsyncSession, project_id: int, track_id: int) -> tuple[Project, Track]:
        p = await s.get(Project, project_id)
        t = await s.get(Track, track_id)
        if p is None or t is None:
            raise MusicError("ролик или трек не найден — начни с /new")
        return p, t

    @staticmethod
    def _apply(p: Project, t: Track, w: Window, length: int) -> None:
        p.track_id, p.no_music = t.id, False
        p.music_start, p.music_end = w.start, w.end
        p.music_parts = None          # один кусок; склейку ставит choose_custom поверх
        p.duration_sec = length
        t.last_used_at = datetime.now(UTC)

    async def next_window(self, project_id: int) -> Choice:
        p = await self.project(project_id)
        if p.track_id is None:
            raise MusicError("сначала выбери трек")
        return await self.choose(project_id, p.track_id, p.music_window + 1)

    async def key_at(self, project_id: int) -> float | None:
        """Секунда главного кадра в ролике — на неё метим дроп. Нет сценария — None."""
        p = await self.project(project_id)
        if not p.script:
            return None
        script = VideoScript.model_validate(p.script)
        return script.start_of(script.key_shot)

    async def no_music(self, project_id: int) -> None:
        async with self.sessions() as s:
            p = await s.get(Project, project_id)
            if p is None:
                raise MusicError("ролик не найден")
            p.track_id, p.no_music = None, True
            p.music_start = p.music_end = None
            p.music_parts = None
            await s.commit()

    async def current(self, project_id: int) -> Choice | None:
        """Выбранная музыка ролика (для сценария на этапе 3)."""
        p = await self.project(project_id)
        if p.track_id is None or p.music_start is None or p.music_end is None:
            return None
        async with self.sessions() as s:
            t = await s.get(Track, p.track_id)
        if t is None:
            return None
        mmap = MusicMap.model_validate(t.analysis)
        if p.music_parts:
            parts = [Window(start=a, end=b, drop_at=segment.drop_in(a, b, mmap))
                     for a, b in p.music_parts]
            return Choice(t, parts[0], p.music_window, p.duration_sec, mmap, parts)
        # Флаг круга в проекте не хранится — выводим его: трек короче ролика обязан
        # крутиться, иначе музыка оборвётся, а без озвучки -shortest обрежет и само видео.
        w = Window(start=p.music_start, end=p.music_end, loop=mmap.duration < p.duration_sec)
        w.drop_at = segment.drop_in(w.start, w.end, mmap)
        return Choice(t, w, p.music_window, p.duration_sec, mmap)

    async def preview(self, choice: Choice) -> bytes:
        """Ровно то, что пойдёт в ролик, с фейдами — послушать до сценария."""
        src = self.media.path(choice.track.file_path)
        if choice.multi:
            return await ffmpeg.audio_parts(src, [(w.start, w.end) for w in choice.parts])
        w = choice.window
        return await ffmpeg.audio_cut(src, w.start, w.end - w.start, loop=w.loop)
