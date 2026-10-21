"""Таблицы. Меняешь модель — добавь миграцию в migrations/versions."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Product(Base):
    """Товар и его карточка. Карточка — JSON по схеме adreel.product.schema.ProductCard."""

    __tablename__ = "products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, index=True)
    # draft — собираем материалы и отвечаем на вопросы; ready — карточка подтверждена
    status: Mapped[str] = mapped_column(String(16), default="draft")
    name: Mapped[str] = mapped_column(String(255), default="")
    card: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # Рыночная выжимка из интернета (research.notes.ResearchNotes): боли, хуки, тренды.
    # Живёт 30 дней, общая для всех роликов товара.
    research: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    last_used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    materials: Mapped[list[Material]] = relationship(
        back_populates="product", cascade="all, delete-orphan", order_by="Material.id")


class Material(Base):
    """Всё, что прислали про товар: ссылка, фото, видео, текст, голосовое, кадр из видео."""

    __tablename__ = "materials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"),
                                            index=True)
    # link | photo | video | text | voice | frame
    kind: Mapped[str] = mapped_column(String(16))
    source: Mapped[str] = mapped_column(Text, default="")      # URL или "telegram"
    text: Mapped[str] = mapped_column(Text, default="")        # текст страницы / расшифровка
    file_path: Mapped[str] = mapped_column(Text, default="")   # относительно workdir
    sha256: Mapped[str] = mapped_column(String(64), default="", index=True)
    mime: Mapped[str] = mapped_column(String(64), default="")
    width: Mapped[int] = mapped_column(Integer, default=0)
    height: Mapped[int] = mapped_column(Integer, default=0)
    # разбор фото зрением, данные страницы (цена, JSON-LD) и т.п.
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    product: Mapped[Product] = relationship(back_populates="materials")


class Track(Base):
    """Трек из библиотеки владельца. Разбор (MusicMap) делается один раз при загрузке."""

    __tablename__ = "tracks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, index=True)
    title: Mapped[str] = mapped_column(String(255), default="")
    source: Mapped[str] = mapped_column(Text, default="telegram")   # telegram или URL
    file_path: Mapped[str] = mapped_column(Text)                    # относительно workdir
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    duration: Mapped[float] = mapped_column(Float, default=0.0)
    bpm: Mapped[float] = mapped_column(Float, default=0.0)
    analysis: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Project(Base):
    """Один ролик: товар + музыка + (дальше) хотелки, сценарий, кадры, клипы."""

    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"),
                                            index=True)
    status: Mapped[str] = mapped_column(String(16), default="draft")
    duration_sec: Mapped[int] = mapped_column(Integer, default=20)
    track_id: Mapped[int | None] = mapped_column(
        ForeignKey("tracks.id", ondelete="SET NULL"), nullable=True)
    no_music: Mapped[bool] = mapped_column(Boolean, default=False)
    # номер подобранного куска (MusicMap.windows[duration][index]) — для «Другой кусок»
    music_window: Mapped[int] = mapped_column(Integer, default=0)
    # выбранный кусок трека, секунды. Задан владельцем («0:42–1:02», «припев») или подобран.
    # Длина куска = длина ролика.
    music_start: Mapped[float | None] = mapped_column(Float, nullable=True)
    music_end: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Креатив: выбранное направление, сценарий видео (кадры), озвучка под кадры.
    # JSON по схемам adreel.script.schema; внутри лежит версия промпта.
    direction: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    script: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    voiceover: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # План оператора: промпты под видеомодель и непрерывность между кадрами
    # (script.shots.ShotPlans)
    shot_plan: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # Готовый смонтированный ролик (путь относительно workdir)
    video_path: Mapped[str] = mapped_column(Text, default="")
    # Сид видеомодели на весь ролик: одинаковый характер картинки во всех сценах и
    # повторяемость при перегенерации. 0 — ещё не выбран.
    seed: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Clip(Base):
    """Один кадр ролика: стартовая картинка и сгенерированный клип.

    Строка на кадр (shot_n). Перегенерация кадра заменяет её содержимое и поднимает
    version — старый файл остаётся на диске, пока его не уберёт уборка.
    """

    __tablename__ = "clips"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"),
                                            index=True)
    shot_n: Mapped[int] = mapped_column(Integer)
    # pending | frame_ready | ready | failed
    status: Mapped[str] = mapped_column(String(16), default="pending")
    frame_path: Mapped[str] = mapped_column(Text, default="")   # стартовый кадр, 9:16
    clip_path: Mapped[str] = mapped_column(Text, default="")    # сгенерированный mp4
    prompt: Mapped[str] = mapped_column(Text, default="")
    model: Mapped[str] = mapped_column(String(64), default="")
    duration: Mapped[float] = mapped_column(Float, default=0.0)  # сколько секунд заказали
    seed: Mapped[int] = mapped_column(Integer, default=0)
    usd: Mapped[float] = mapped_column(Float, default=0.0)
    error: Mapped[str] = mapped_column(Text, default="")
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class CostEventRow(Base):
    __tablename__ = "cost_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int | None] = mapped_column(
        ForeignKey("products.id", ondelete="SET NULL"), index=True, nullable=True)
    stage: Mapped[str] = mapped_column(String(32), default="")
    provider: Mapped[str] = mapped_column(String(32))
    operation: Mapped[str] = mapped_column(String(128))
    units: Mapped[float] = mapped_column(Float)
    unit_type: Mapped[str] = mapped_column(String(32))
    usd: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
