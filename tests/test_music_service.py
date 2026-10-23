"""Музыка: кусок словами владельца и сервис целиком (SQLite + настоящий разбор и ffmpeg)."""

from __future__ import annotations

import io
import wave

import numpy as np
import pytest
from test_music_analyze import click_track  # tests/ в sys.path (режим prepend у pytest)

from adreel.db.models import Material
from adreel.db.session import make_engine, make_sessions, upgrade
from adreel.ingest import video
from adreel.ingest.media import MediaStore
from adreel.music import segment
from adreel.music.analyze import MusicError, MusicMap, analyze
from adreel.music.service import MusicService, PendingTrack
from adreel.product.service import Deps, ProductService
from adreel.render import ffmpeg


@pytest.fixture(scope="module")
def mmap() -> MusicMap:
    return analyze(click_track(90, drop_at=40))


@pytest.mark.parametrize(("text", "start", "length"), [
    ("0:42–1:02", 42, 20),
    ("с 30 по 45", 30, 15),
    ("с 1:00 на 12 секунд", 60, 12),
    ("вступление", 0, 20),
    ("концовка", 70, 20),
])
def test_segment_by_words(mmap, text, start, length):
    seg = segment.parse(text, mmap, default_len=20)
    assert seg.window.start == pytest.approx(start, abs=0.4)   # может сдвинуть к доле
    assert seg.window.end - seg.window.start == pytest.approx(length, abs=0.01)


def test_chorus_takes_the_drop(mmap):
    seg = segment.parse("припев", mmap)
    assert seg.window.drop_at is not None
    assert "дроп" in seg.note


def test_whole_track_is_capped_to_30(mmap):
    seg = segment.parse("весь трек", mmap)
    assert seg.window.end - seg.window.start == 30
    assert "30" in seg.note


def test_segment_edge_cases(mmap):
    assert "сдвинул" in segment.parse("1:25–1:45", mmap).note         # упёрлись в конец
    assert "растянул" in segment.parse("0:10–0:12", mmap).note
    with pytest.raises(MusicError, match="Примеры"):
        segment.parse("как-нибудь", mmap)


def test_several_parts_by_words(mmap):
    """«а в конце» между диапазонами — не просьба взять концовку трека."""
    parts = segment.parse_parts("с 10 по 30 один кусок, а в конце с 70 по 80", mmap)
    assert parts is not None and len(parts.windows) == 2
    first, last = parts.windows
    assert first.start == pytest.approx(10, abs=0.4) and last.start == pytest.approx(70, abs=0.4)
    assert first.end - first.start == pytest.approx(20)
    assert last.end - last.start == pytest.approx(10)
    assert parts.length == pytest.approx(30)
    assert segment.parse_parts("0:42–1:02", mmap) is None          # один кусок — обычный разбор


def test_parts_longer_than_a_video_are_trimmed(mmap):
    parts = segment.parse_parts("0-20 и 40-60", mmap)
    assert parts is not None and parts.length == pytest.approx(30)
    assert "укоротил" in parts.note
    with pytest.raises(MusicError, match="мало"):
        segment.parse_parts("с 10 по 12 и с 20 по 22", mmap)
    with pytest.raises(MusicError, match="обрывок"):
        segment.parse_parts("с 10 по 11 и с 20 по 30", mmap)


def test_parts_fit_the_script_by_the_last_piece(mmap):
    parts = segment.parse_parts("10–30, 70–80", mmap)
    assert parts is not None
    fitted = segment.fit_parts(parts, 25, mmap)
    assert fitted.length == pytest.approx(25)
    assert fitted.windows[0] == parts.windows[0]               # первый кусок как назвали
    assert "подогнал" in fitted.note


def wav_bytes(y: np.ndarray) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(22050)
        w.writeframes((np.clip(y, -1, 1) * 32767).astype(np.int16).tobytes())
    return buf.getvalue()


@pytest.fixture
async def services(tmp_path):
    url = f"sqlite+aiosqlite:///{(tmp_path / 'm.db').as_posix()}"
    upgrade(url)
    engine = make_engine(url)
    sessions = make_sessions(engine)
    media = MediaStore(tmp_path)
    products = ProductService(Deps(sessions=sessions, media=media, text_llm=lambda: None,
                                   vision_llm=lambda: None, stt=lambda: None))
    yield products, MusicService(sessions, media)
    await engine.dispose()


@pytest.mark.ffmpeg
async def test_upload_rights_choose_and_preview(services):
    products, music = services
    pid = await products.new_draft(1, "uk")
    project_id = await music.start_project(pid)
    data = wav_bytes(click_track(60, drop_at=24))

    pending = await music.prepare_upload(1, data, "beat.wav")
    assert isinstance(pending, PendingTrack) and pending.title == "beat"
    assert pending.map.bpm == pytest.approx(120, abs=2)
    track = await music.save_track(1, pending)

    again = await music.prepare_upload(1, data, "beat-copy.wav")   # тот же файл — из библиотеки
    assert not isinstance(again, PendingTrack) and again.id == track.id
    assert [t.id for t in await music.tracks(1)] == [track.id]

    choice = await music.choose(project_id, track.id)
    assert choice.window.end - choice.window.start == 20
    other = await music.next_window(project_id)
    assert other.window.start != choice.window.start

    custom, _ = await music.choose_custom(project_id, track.id, "с 10 по 25")
    assert custom.duration == 15
    assert (await music.project(project_id)).duration_sec == 15
    current = await music.current(project_id)
    assert current is not None and current.window.start == pytest.approx(10, abs=0.4)

    mp3 = await music.preview(custom)
    assert await ffmpeg.audio_duration(mp3) == pytest.approx(15, abs=0.2)

    await music.no_music(project_id)
    assert await music.current(project_id) is None


@pytest.mark.ffmpeg
async def test_several_parts_become_one_soundtrack(services):
    products, music = services
    project_id = await music.start_project(await products.new_draft(1, "uk"))
    pending = await music.prepare_upload(1, wav_bytes(click_track(90, drop_at=40)), "beat.wav")
    track = await music.save_track(1, pending)

    choice, _ = await music.choose_custom(project_id, track.id, "с 10 по 30, а в конце с 70 по 80")
    assert choice.multi and choice.duration == 30
    assert (await music.project(project_id)).duration_sec == 30

    current = await music.current(project_id)
    assert current is not None and len(current.parts) == 2      # пережило БД
    mp3 = await music.preview(current)
    assert await ffmpeg.audio_duration(mp3) == pytest.approx(30, abs=0.2)

    # один кусок снова — склейка забыта
    single, _ = await music.choose_custom(project_id, track.id, "с 10 по 30")
    assert not single.multi
    again = await music.current(project_id)
    assert again is not None and not again.multi


async def test_music_link_only_youtube(services):
    _, music = services
    with pytest.raises(MusicError, match="YouTube"):
        await music.prepare_link(1, "https://www.tiktok.com/@x/video/1")


async def _saved(music: MusicService, mmap: MusicMap, data: bytes = b"track",
                 title: str = "Трек с YouTube"):
    """Трек в библиотеке без librosa и ffmpeg: разбор берём готовый."""
    stored = music.media.save(data, "audio/mpeg")
    return await music.save_track(1, PendingTrack(stored.rel_path, stored.sha256, title,
                                                  "telegram", mmap.model_dump()))


async def test_short_track_loops_under_the_whole_video(services):
    """Трек короче ролика: без круга музыка обрывалась, а без голоса -shortest резал видео."""
    products, music = services
    project_id = await music.start_project(await products.new_draft(1, "uk"))
    track = await _saved(music, analyze(click_track(10, drop_at=5)))
    choice = await music.choose(project_id, track.id)
    assert choice.duration == 20 and choice.window.loop
    current = await music.current(project_id)
    assert current is not None and current.window.loop


async def test_rename_track(services, mmap):
    _, music = services
    track = await _saved(music, mmap)

    renamed = await music.rename_track(1, track.id, "  Lo-fi\n для  кофе ")
    assert renamed.title == "Lo-fi для кофе"            # одна строка: идёт в кнопку
    assert (await music.tracks(1))[0].title == "Lo-fi для кофе"

    with pytest.raises(MusicError, match="пустое"):
        await music.rename_track(1, track.id, "   ")
    with pytest.raises(MusicError, match="не найден"):
        await music.rename_track(2, track.id, "чужой")      # не владелец


async def test_delete_track_frees_project_and_file(services, mmap):
    products, music = services
    project_id = await music.start_project(await products.new_draft(1, "uk"))
    track = await _saved(music, mmap)
    await music.choose(project_id, track.id)
    assert await music.count_tracks(1) == 1

    assert await music.delete_track(1, track.id) == "Трек с YouTube"

    p = await music.project(project_id)
    assert p.track_id is None and p.music_start is None and p.music_end is None
    assert await music.current(project_id) is None
    assert await music.count_tracks(1) == 0
    assert not music.media.path(track.file_path).exists()
    with pytest.raises(MusicError, match="не найден"):
        await music.delete_track(1, track.id)


async def test_delete_track_keeps_shared_file(services, mmap):
    """Файлы адресуются по sha256: тот же файл у материала товара не трогаем."""
    products, music = services
    pid = await products.new_draft(1, "uk")
    track = await _saved(music, mmap)
    async with music.sessions() as s:
        s.add(Material(product_id=pid, kind="voice", file_path=track.file_path))
        await s.commit()

    await music.delete_track(1, track.id)
    assert music.media.path(track.file_path).exists()


@pytest.mark.ffmpeg
async def test_link_track_named_after_video(services, mmap):
    _, base = services

    async def download(url: str) -> video.Downloaded:
        return video.Downloaded(wav_bytes(click_track(5, drop_at=3)),
                                "Daft Punk — Around the World")

    async def analyze_fake(data: bytes) -> MusicMap:
        return mmap

    music = MusicService(base.sessions, base.media, download_video=download,
                         analyze=analyze_fake)
    pending = await music.prepare_link(1, "https://www.youtube.com/watch?v=x")
    assert isinstance(pending, PendingTrack)
    assert pending.title == "Daft Punk — Around the World"
