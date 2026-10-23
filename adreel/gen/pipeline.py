"""Генерация ролика по сценам: стартовая картинка → клип → следующая сцена → монтаж.

Порядок важен. Сцена, которая «продолжает предыдущую», стартует с последнего кадра уже
готового клипа, поэтому сцены делаются строго по очереди, а не пачкой.

Владельцу куски не показываем: он ждёт один готовый ролик, а не пазл из клипов.

Группы. Оператор может пометить соседние сцены как одну генерацию: модель режет внутри
клипа сама, эти склейки самые честные (свет и мир общие), и платим мы один раз. У модели
минимум 5 секунд на вызов, поэтому две короткие сцены одной генерацией заметно дешевле.
Готовый клип режем на куски по сценам — дальше всё работает как со обычными клипами.

Деньги. Каждый клип оплачивается, поэтому:
  • перед стартом показываем оценку;
  • каждый готовый клип сразу пишется в БД — падение на третьей сцене не сжигает первые две;
  • повтор («доделать») пропускает готовые сцены, а не генерит их заново;
  • стартовый кадр пишется в БД ДО вызова видеомодели (frame_ready): картинка уже
    оплачена, и с ней совпадёт отпечаток клипа — зависший оплаченный запрос подберётся
    (gen/pending.py), а не оплатится второй раз;
  • один ролик не генерится дважды параллельно (двойной тап по кнопке).
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .. import costs
from ..db.models import Clip, Material, Project
from ..ingest.media import MediaStore
from ..music.service import MusicService
from ..product.service import ProductService
from ..progress import say
from ..render import ffmpeg, montage
from ..script.schema import Shot, VideoScript
from ..script.service import ScriptService
from ..script.shots import ShotPlan, ShotPlans
from ..tts.base import TTSProvider
from . import frames
from .base import ImageGenProvider, ImageInput, Progress, VideoGenProvider
from .models import ImageModel, VideoModel, video_model

log = logging.getLogger("adreel.gen.pipeline")


class GenerationError(RuntimeError):
    """Ошибка, которую можно показать владельцу."""


@dataclass
class Estimate:
    scenes: int          # сколько картинок генерируем (сцены с товаром)
    clips: int           # сколько клипов
    seconds: int         # суммарные секунды клипов
    usd: float
    seamless: int = 0     # сколько стыков делаем дорогим бесшовным продолжением
    generations: int = 0  # сколько вызовов модели (сцены могут сниматься группами)

    @property
    def text(self) -> str:
        if not self.clips:
            return "все сцены уже готовы, осталось склеить ≈ $0.00"
        seamless = f", бесшовных стыков {self.seamless}" if self.seamless else ""
        how = (f"{self.clips} сцен" if self.generations in (0, self.clips)
               else f"{self.clips} сцен за {self.generations} генераций")
        return (f"{how} на {self.seconds} с и {self.scenes} картинок{seamless} "
                f"≈ ${self.usd:.2f}")


@dataclass
class ClipResult:
    shot_n: int
    clip: bytes
    duration: float
    usd: float
    note: str = ""


@dataclass
class Deps:
    sessions: async_sessionmaker[AsyncSession]
    media: MediaStore
    products: ProductService
    scripts: ScriptService
    imagegen: Callable[[], ImageGenProvider]
    videogen: Callable[[], VideoGenProvider]
    image_model: Callable[[], ImageModel]
    video_model: Callable[[], VideoModel]
    music: MusicService
    tts: Callable[[], TTSProvider]
    fonts_dir: Path


@dataclass
class _Group:
    """Одна генерация: одна или несколько подряд идущих сцен."""

    shots: list[Shot]
    plans: list[ShotPlan]
    duration: int                  # сколько секунд заказываем у модели
    offsets: list[float]           # начало каждой сцены от начала генерации

    @property
    def lead(self) -> Shot:
        return self.shots[0]

    @property
    def lead_plan(self) -> ShotPlan:
        return self.plans[0]

    @property
    def numbers(self) -> list[int]:
        return [x.n for x in self.shots]


@dataclass
class _Plan:
    project: Project
    script: VideoScript
    plans: ShotPlans
    photos: dict[int, bytes] = field(default_factory=dict)
    #: первый кадр ролика — эталон палитры для продолжающих сцен (против дрейфа цвета)
    anchor: bytes | None = None


#: статусы, при которых у сцены есть оплаченный стартовый кадр, но нет готового клипа
_FRAME_KEPT = ("frame_ready", "failed")


def _has_frame(row: Clip | None) -> bool:
    return bool(row and row.frame_path and row.status in _FRAME_KEPT)


class GenerationService:
    def __init__(self, deps: Deps) -> None:
        self.d = deps
        #: ролики, которые генерируются прямо сейчас. Проверка и запись без await
        #: между ними — в asyncio это атомарно, второй запуск отлетит сразу.
        self._running: set[int] = set()

    # ---------- оценка ----------

    async def estimate(self, project_id: int) -> Estimate:
        """Сколько стоит доделать ролик: готовые сцены и оплаченные кадры не считаем."""
        plan = await self._load(project_id, with_photos=False)
        vmodel, imodel = self.d.video_model(), self.d.image_model()
        chain = self._chain_model(vmodel)
        rows = await self._rows(project_id)
        groups = self._not_ready(self._groups(plan, vmodel), rows)
        seconds = scenes = seamless = 0
        usd = 0.0
        for group in groups:
            seconds += group.duration
            if self._is_reference(group.lead_plan, vmodel) and chain is not None:
                usd += chain.price(group.duration, with_reference=True)
                seamless += 1
            else:
                usd += vmodel.price(group.duration)
            # картинку готовим только лиду группы: у остальных сцен своего старта нет
            if (group.lead_plan.start_from == "generated_scene"
                    and not _has_frame(rows.get(group.lead.n))):
                scenes += 1
        usd += scenes * imodel.price_per_image
        return Estimate(scenes, sum(len(g.shots) for g in groups), seconds, round(usd, 2),
                        seamless, len(groups))

    @staticmethod
    def _not_ready(groups: list[_Group], rows: dict[int, Clip]) -> list[_Group]:
        return [g for g in groups
                if not all(n in rows and rows[n].status == "ready" for n in g.numbers)]

    @staticmethod
    def _is_reference(sp: ShotPlan, vmodel: VideoModel) -> bool:
        """Делаем ли этот стык дорогим бесшовным продолжением по хвосту клипа."""
        return (sp.start_from == "prev_last_frame" and sp.chain == "reference"
                and bool(vmodel.chain_model))

    def _chain_model(self, vmodel: VideoModel) -> VideoModel | None:
        return video_model(vmodel.chain_model) if vmodel.chain_model else None

    @staticmethod
    def _groups(plan: _Plan, vmodel: VideoModel) -> list[_Group]:
        """Разложить сценарий по генерациям так, как решил оператор."""
        by_n = {s.n: s for s in plan.script.shots}
        out: list[_Group] = []
        for plans in plan.plans.groups():
            shots = [by_n[x.n] for x in plans if x.n in by_n]
            if not shots:
                continue
            offsets, at = [], 0.0
            for sh in shots:
                offsets.append(round(at, 3))
                at += sh.duration_sec
            out.append(_Group(shots, plans[:len(shots)],
                              vmodel.clip_duration(sum(x.duration_sec for x in shots)),
                              offsets))
        return out

    # ---------- генерация ----------

    async def generate(self, project_id: int, *, only: int | None = None,
                       progress: Progress | None = None) -> None:
        """Доснять недостающие генерации (или переснять ту, где живёт сцена only).
        Дальше склеит assemble().

        Без only это «доделать»: готовые сцены не трогаем и второй раз не оплачиваем.
        Переснять всё — сначала drop()."""
        if project_id in self._running:
            raise GenerationError("этот ролик уже генерируется — дождись результата")
        self._running.add(project_id)
        try:
            await self._generate(project_id, only, progress)
        finally:
            self._running.discard(project_id)

    async def _generate(self, project_id: int, only: int | None,
                        progress: Progress | None) -> None:
        plan = await self._load(project_id)
        vmodel = self.d.video_model()
        groups = self._groups(plan, vmodel)
        if only is not None:
            # сцену из группы нельзя переснять в одиночку: генерация общая
            groups = [g for g in groups if only in g.numbers]
            if not groups:
                raise GenerationError(f"сцены {only} нет в сценарии")
        else:
            rows = await self._rows(project_id)
            todo = self._not_ready(groups, rows)
            # Первая генерация уже готова — её кадр остаётся якорем палитры: продолжающие
            # сцены после сбоя должны тянуться к тому же цвету, что и до него.
            first = rows.get(groups[0].lead.n) if groups else None
            if first and first.frame_path and not any(g is groups[0] for g in todo):
                plan.anchor = await self.d.media.read_async(first.frame_path)
            groups = todo

        seed = await self._seed(plan.project)
        total = len(plan.script.shots)
        for group in groups:
            sp = group.lead_plan
            what = ("продолжаю предыдущую сцену" if sp.start_from == "prev_last_frame"
                    else "готовлю картинку")
            names = ", ".join(str(n) for n in group.numbers)
            title = f"Сцена {names}" if len(group.shots) == 1 else f"Сцены {names} одним куском"
            await say(progress, f"🎞 {title} из {total}: {what}…")
            async with self.d.products.spend(plan.project.product_id, "generate") as spent:
                try:
                    await self._one_group(plan, group, vmodel, seed, spent, progress,
                                          resume=only is None)
                except Exception as exc:
                    for sh in group.shots:
                        await self._save_failed(project_id, sh.n, str(exc)[:500])
                    raise GenerationError(f"сцена {group.lead.n}: {str(exc)[:200]}") from exc

    async def _one_group(self, plan: _Plan, group: _Group, vmodel: VideoModel,
                         seed: int, spent: costs.Ledger, progress: Progress | None,
                         resume: bool = True) -> list[ClipResult]:
        """spent — журнал products.spend: всё, что потратим, уйдёт в cost_events.
        Свой вложенный журнал тут нельзя: он подменил бы этот, и стоимость видео
        не попала бы в учёт. Цену клипа считаем разницей журнала до и после."""
        videogen = self.d.videogen()
        sp, shot, duration = group.lead_plan, group.lead, group.duration
        # сид ролика + номер первой сцены: характер картинки общий, генерации разные,
        # повтор той же генерации даёт тот же результат
        shot_seed = seed + shot.n
        seamless = self._is_reference(sp, vmodel) and videogen.can_continue

        prev = await self._clip_bytes(plan.project.id, shot.n - 1) if seamless else None
        if seamless and prev is not None:
            chain = self._chain_model(vmodel)
            tail_sec = chain.reference_video_sec if chain else 1.0
            await say(progress, f"🎬 Сцена {shot.n}: бесшовно продолжаю предыдущую "
                                 f"({duration} с)…")
            tail = await frames.tail_clip(prev, tail_sec)
            refs = [ImageInput(await frames.vertical(self._photo(plan, shot)), "image/jpeg")]
            prompt = plan.plans.group_prompt(group.plans, group.offsets, duration=duration,
                                             reference=True)
            frame = await frames.last_frame(prev, plan.anchor)   # кадр для отчётности и стыка
            before = spent.total_usd
            clip = await videogen.continue_video(tail, refs, prompt, duration,
                                                 seed=shot_seed, progress=progress)
            model_key = chain.key if chain else vmodel.key
        else:
            cached = await self._cached_frame(plan.project.id, shot.n) if resume else None
            if cached is not None:
                frame = cached
                await say(progress, f"🖼 Сцена {shot.n}: картинка с прошлой попытки — "
                                     "беру её, без новой оплаты")
            else:
                frame = await self._start_frame(plan, shot, sp, progress)
                await self._save_frame(plan.project.id, shot.n, frame)
            # конечный кадр прибивается к финалу ПОСЛЕДНЕЙ сцены генерации
            end_image = await self._end_frame(plan, group.shots[-1], group.plans[-1])
            prompt = plan.plans.group_prompt(group.plans, group.offsets, duration=duration,
                                             end_image=end_image is not None)
            await say(progress, f"🎬 Сцена {shot.n}: генерирую {duration} с на {vmodel.title}…")
            before = spent.total_usd
            clip = await videogen.image_to_video(
                ImageInput(frame, "image/jpeg"), prompt, duration,
                end_image=end_image, seed=shot_seed, progress=progress)
            model_key = vmodel.key

        if plan.anchor is None:
            plan.anchor = frame          # первый кадр ролика задаёт палитру остальным
        return await self._save_group(plan, group, frame, clip, prompt, model_key,
                                      spent.total_usd - before, shot_seed)

    async def _save_group(self, plan: _Plan, group: _Group, frame: bytes, clip: bytes,
                          prompt: str, model_key: str, usd: float,
                          seed: int) -> list[ClipResult]:
        """Разложить генерацию по сценам: каждая получает свой кусок клипа.

        Модель режет по нашим таймкодам, но попадает в них не идеально — кусок берём
        по плану сценария, лишний кадр на стыке всё равно срежет монтаж.
        """
        pieces = await self._split(clip, group)
        share = round(usd / len(group.shots), 6)
        out: list[ClipResult] = []
        for i, shot in enumerate(group.shots):
            sp = group.plans[i]
            await self._save_ready(plan.project.id, shot.n, frame, pieces[i], prompt,
                                   model_key, group.duration, share, seed)
            note = ("снята вместе с предыдущей" if sp.group_with_prev else
                    "продолжает предыдущую сцену" if sp.start_from == "prev_last_frame" else "")
            out.append(ClipResult(shot.n, pieces[i], shot.duration_sec, share, note))
        return out

    async def _split(self, clip: bytes, group: _Group) -> list[bytes]:
        if len(group.shots) == 1:
            return [clip]               # одна сцена — клип идёт в монтаж как есть
        return [await ffmpeg.cut_clip(clip, start, shot.duration_sec)
                for start, shot in zip(group.offsets, group.shots, strict=True)]

    async def _seed(self, project: Project) -> int:
        """Сид ролика: один на все сцены, живёт в проекте."""
        if project.seed:
            return project.seed
        seed = secrets.randbelow(2**31 - 100)
        async with self.d.sessions() as s:
            row = await s.get(Project, project.id)
            if row is not None:
                row.seed = seed
                await s.commit()
        project.seed = seed
        return seed

    async def _start_frame(self, plan: _Plan, shot: Shot, sp: ShotPlan,
                           progress: Progress | None) -> bytes:
        if sp.start_from == "prev_last_frame":
            prev = await self._clip_bytes(plan.project.id, shot.n - 1)
            if prev is not None:
                # палитру тянем к первому кадру ролика: иначе цепочка уползает в цвете
                return await frames.last_frame(prev, plan.anchor)
            log.warning("сцена %s: предыдущего клипа нет, беру фото товара", shot.n)
        photo = self._photo(plan, shot)
        if sp.start_from == "generated_scene":
            return await frames.make_scene(self.d.imagegen(), photo, shot.visual, progress)
        return await frames.vertical(photo)

    async def _end_frame(self, plan: _Plan, shot: Shot, sp: ShotPlan) -> ImageInput | None:
        """Конечный кадр: прибиваем финал клипа к нужной картинке (модель дорисует переход)."""
        if sp.end_with == "product_photo":
            return ImageInput(await frames.vertical(self._photo(plan, shot)), "image/jpeg")
        if sp.end_with == "next_start":
            nxt = next((s for s in plan.script.shots if s.n == shot.n + 1), None)
            if nxt is None:
                return None
            data = await self._frame_bytes(plan.project.id, nxt.n)
            if data is None:
                data = await frames.vertical(self._photo(plan, nxt))
            return ImageInput(data, "image/jpeg")
        return None

    def _photo(self, plan: _Plan, shot: Shot) -> bytes:
        if shot.photo_ref and shot.photo_ref in plan.photos:
            return plan.photos[shot.photo_ref]
        if plan.photos:
            return next(iter(plan.photos.values()))
        raise GenerationError("нет ни одного фото товара — пришли фото в /new")

    # ---------- БД ----------

    async def _load(self, project_id: int, with_photos: bool = True) -> _Plan:
        c = await self.d.scripts.get(project_id)
        if c.script is None:
            raise GenerationError("нет сценария — начни с /new")
        if c.shot_plan is None:
            raise GenerationError("нет заданий для видеомодели — утверди сценарий заново")
        plan = _Plan(c.project, c.script, c.shot_plan)
        if with_photos:
            async with self.d.sessions() as s:
                rows = await s.scalars(select(Material).where(
                    Material.product_id == c.project.product_id, Material.kind == "photo"))
                plan.photos = {m.id: await self.d.media.read_async(m.file_path) for m in rows
                               if m.file_path}
        return plan

    async def clips(self, project_id: int) -> list[Clip]:
        async with self.d.sessions() as s:
            rows = await s.scalars(select(Clip).where(Clip.project_id == project_id)
                                   .order_by(Clip.shot_n))
            return list(rows)

    async def _rows(self, project_id: int) -> dict[int, Clip]:
        return {c.shot_n: c for c in await self.clips(project_id)}

    async def _cached_frame(self, project_id: int, shot_n: int) -> bytes | None:
        """Стартовый кадр прошлой попытки, у которой не вышел клип."""
        row = await self._row(project_id, shot_n)
        return await self.d.media.read_async(row.frame_path) if row and _has_frame(row) else None

    async def _save_frame(self, project_id: int, shot_n: int, frame: bytes) -> None:
        stored = await self.d.media.save_async(frame, "image/jpeg")
        async with self.d.sessions() as s:
            row = await s.scalar(select(Clip).where(Clip.project_id == project_id,
                                                    Clip.shot_n == shot_n))
            if row is None:
                row = Clip(project_id=project_id, shot_n=shot_n)
                s.add(row)
            row.status, row.error, row.frame_path = "frame_ready", "", stored.rel_path
            await s.commit()

    async def ready(self, project_id: int) -> bool:
        plan = await self._load(project_id, with_photos=False)
        done = {c.shot_n for c in await self.clips(project_id) if c.status == "ready"}
        return {s.n for s in plan.script.shots} <= done

    async def _clip_bytes(self, project_id: int, shot_n: int) -> bytes | None:
        row = await self._row(project_id, shot_n)
        return await self.d.media.read_async(row.clip_path) if row and row.clip_path else None

    async def _frame_bytes(self, project_id: int, shot_n: int) -> bytes | None:
        row = await self._row(project_id, shot_n)
        return await self.d.media.read_async(row.frame_path) if row and row.frame_path else None

    async def _row(self, project_id: int, shot_n: int) -> Clip | None:
        async with self.d.sessions() as s:
            return await s.scalar(select(Clip).where(Clip.project_id == project_id,
                                                     Clip.shot_n == shot_n))

    async def _save_ready(self, project_id: int, shot_n: int, frame: bytes, clip: bytes,
                          prompt: str, model: str, duration: int, usd: float,
                          seed: int = 0) -> None:
        frame_stored = await self.d.media.save_async(frame, "image/jpeg")
        clip_stored = await self.d.media.save_async(clip, "video/mp4")
        async with self.d.sessions() as s:
            row = await s.scalar(select(Clip).where(Clip.project_id == project_id,
                                                    Clip.shot_n == shot_n))
            if row is None:
                row = Clip(project_id=project_id, shot_n=shot_n)
                s.add(row)
            elif row.clip_path:
                row.version += 1        # версия клипа, а строку мог завести ещё кадр
            row.status, row.error = "ready", ""
            row.frame_path, row.clip_path = frame_stored.rel_path, clip_stored.rel_path
            row.prompt, row.model, row.duration, row.usd = prompt, model, duration, usd
            row.seed = seed
            await s.commit()

    async def _save_failed(self, project_id: int, shot_n: int, error: str) -> None:
        async with self.d.sessions() as s:
            row = await s.scalar(select(Clip).where(Clip.project_id == project_id,
                                                    Clip.shot_n == shot_n))
            if row is None:
                row = Clip(project_id=project_id, shot_n=shot_n)
                s.add(row)
            row.status, row.error = "failed", error
            await s.commit()

    # ---------- монтаж ----------

    async def assemble(self, project_id: int, progress: Progress | None = None) -> bytes:
        """Склеить готовые клипы в ролик: озвучка, субтитры, музыка тише голоса."""
        plan = await self._load(project_id, with_photos=False)
        rows = {c.shot_n: c for c in await self.clips(project_id) if c.status == "ready"}
        missing = [s.n for s in plan.script.shots if s.n not in rows]
        if missing:
            raise GenerationError(f"нет клипов для сцен {missing} — сгенерируй заново")

        shots = [montage.ShotClip(
            n=s.n, path=self.d.media.path(rows[s.n].clip_path), duration=s.duration_sec,
            chained=(plan.plans.plan_for(s.n).start_from == "prev_last_frame"
                     and not plan.plans.plan_for(s.n).group_with_prev),
            on_screen_text=s.on_screen_text) for s in plan.script.shots]

        await say(progress, "🎙 Озвучиваю и клею ролик…")
        workdir = self.d.media.workdir / "render" / f"project{project_id}"
        c = await self.d.scripts.get(project_id)
        async with self.d.products.spend(plan.project.product_id, "montage"):
            result = await montage.build(
                shots, plan.script, c.voiceover, await self._music_part(plan.project),
                self.d.tts() if c.voiceover else None, workdir, fonts_dir=self.d.fonts_dir)

        stored = await self.d.media.save_async(result.video, "video/mp4")
        async with self.d.sessions() as s:
            row = await s.get(Project, project_id)
            if row is not None:
                row.video_path = stored.rel_path
                await s.commit()
        return result.video

    async def _music_part(self, project: Project) -> montage.MusicPart | None:
        choice = await self.d.music.current(project.id)
        if choice is None:
            return None
        w = choice.window
        parts = [(p.start, p.end) for p in choice.parts] if choice.multi else []
        return montage.MusicPart(self.d.media.path(choice.track.file_path), w.start,
                                 w.end - w.start, loop=w.loop, parts=parts)

    async def drop(self, project_id: int, *, reseed: bool = False) -> None:
        """Сценарий поменялся — старые клипы больше не подходят.

        reseed — «заново целиком»: с тем же сидом и теми же кадрами модель выдала бы
        почти то же самое, за что владелец только что заплатил."""
        if project_id in self._running:
            raise GenerationError("ролик ещё генерируется — дождись результата")
        async with self.d.sessions() as s:
            await s.execute(delete(Clip).where(Clip.project_id == project_id))
            if reseed:
                row = await s.get(Project, project_id)
                if row is not None:
                    row.seed = 0
            await s.commit()
