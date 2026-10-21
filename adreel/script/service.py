"""Креатив ролика: направление → длина → сценарий видео → озвучка. Хранится в projects."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .. import pace
from ..db.models import Product, Project
from ..gen.models import VideoModel, video_model
from ..llm.base import LLMProvider
from ..product.schema import ProductCard
from ..product.service import ProductService
from ..research.notes import ResearchNotes, collect
from ..research.search import SearchProvider, make_search
from . import creative, shots, validate
from .creative import Brief, PhotoInfo, ScriptError
from .schema import Direction, Directions, VideoScript, Voiceover
from .shots import ShotPlans

DURATIONS = (15, 20, 30)


@dataclass
class Creative:
    project: Project
    direction: Direction | None
    script: VideoScript | None
    voiceover: Voiceover | None
    shot_plan: ShotPlans | None


class ScriptService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        products: ProductService,
        llm: Callable[[], LLMProvider],
        model: Callable[[], VideoModel] = video_model,
        chars_per_sec: Callable[[], float] = pace.chars_per_sec,
        search: Callable[[], SearchProvider | None] = make_search,
    ) -> None:
        self.sessions = sessions
        self.products = products
        self.llm = llm
        self.model = model
        self.chars_per_sec = chars_per_sec
        self.search = search

    # ---------- ролик ----------

    async def start_project(self, product_id: int) -> int:
        async with self.sessions() as s:
            p = Project(product_id=product_id)
            s.add(p)
            await s.commit()
            return p.id

    async def get(self, project_id: int) -> Creative:
        async with self.sessions() as s:
            p = await s.get(Project, project_id)
            if p is None:
                raise ScriptError("ролик не найден — начни с /new")
        return Creative(
            p,
            Direction.model_validate(p.direction) if p.direction else None,
            VideoScript.model_validate(p.script) if p.script else None,
            Voiceover.model_validate(p.voiceover) if p.voiceover else None,
            ShotPlans.model_validate(p.shot_plan) if p.shot_plan else None,
        )

    async def _update(self, project_id: int, **fields: object) -> None:
        async with self.sessions() as s:
            p = await s.get(Project, project_id)
            if p is None:
                raise ScriptError("ролик не найден — начни с /new")
            for k, v in fields.items():
                setattr(p, k, v)
            await s.commit()

    async def brief(self, product_id: int) -> Brief:
        card = await self.products.card(product_id)
        photos = [PhotoInfo(m.id, _photo_desc(m.meta, m.width, m.height))
                  for m in await self.products.product_photos(product_id)]
        notes = await self.market_notes(product_id, card)
        return Brief(card, photos, await self.products.owner_notes(product_id),
                     market=notes.block() if notes else "")

    async def market_notes(self, product_id: int,
                           card: ProductCard) -> ResearchNotes | None:
        """Выжимка про рынок из интернета. Живёт 30 дней и общая для всех роликов товара.

        Поиска нет (нет ключа) или он упал — работаем без него: шаг необязательный.
        """
        async with self.sessions() as s:
            product = await s.get(Product, product_id)
            saved = product.research if product else None
        if saved:
            notes = ResearchNotes.model_validate(saved)
            if notes.fresh():
                return notes
        provider = self.search()
        if provider is None:
            return None
        async with self.products.spend(product_id, "research"):
            notes = await collect(provider, self.llm(), card)
        if notes.empty:
            return None
        async with self.sessions() as s:
            product = await s.get(Product, product_id)
            if product is not None:
                product.research = notes.model_dump()
                await s.commit()
        return notes

    # ---------- направления ----------

    async def directions(self, project_id: int, *, wishes: str = "",
                         avoid: list[Direction] | None = None) -> Directions:
        c = await self.get(project_id)
        brief = await self.brief(c.project.product_id)
        async with self.products.spend(c.project.product_id, "creative"):
            return await creative.propose_directions(self.llm(), brief, wishes=wishes,
                                                     avoid=avoid)

    async def own_direction(self, project_id: int, idea: str) -> Direction:
        c = await self.get(project_id)
        brief = await self.brief(c.project.product_id)
        async with self.products.spend(c.project.product_id, "creative"):
            return await creative.own_direction(self.llm(), brief, idea)

    async def choose_direction(self, project_id: int, direction: Direction) -> None:
        # новое направление — старые сценарий и озвучка больше не подходят
        await self._update(project_id, direction=direction.model_dump(), script=None,
                           voiceover=None)

    async def set_duration(self, project_id: int, seconds: int) -> None:
        if seconds not in DURATIONS:
            raise ScriptError(f"длина ролика — {', '.join(map(str, DURATIONS))} с")
        await self._update(project_id, duration_sec=seconds, script=None, voiceover=None)

    # ---------- сценарий видео ----------

    async def make_script(self, project_id: int, kind: str = "new", text: str = "",
                          shot: int | None = None) -> VideoScript:
        """kind: new — с нуля; edit — правка словами; redo — переделать кадр; variant — другой."""
        c = await self.get(project_id)
        if c.direction is None:
            raise ScriptError("сначала выбери направление ролика")
        brief = await self.brief(c.project.product_id)
        rules = validate.ScriptRules.for_duration(
            c.project.duration_sec, {p.id for p in brief.photos}, self.model().max_sec)
        instruction = creative.script_instruction(kind, c.script, text, shot)
        async with self.products.spend(c.project.product_id, "script"):
            script = await creative.write_script(self.llm(), brief, c.direction, rules,
                                                 instruction=instruction)
        # сценарий поменялся — озвучка и план оператора под старые кадры устарели
        await self._update(project_id, script=script.model_dump(), voiceover=None, shot_plan=None)
        return script

    # ---------- озвучка ----------

    async def make_voiceover(self, project_id: int, kind: str = "new",
                             text: str = "") -> Voiceover:
        c = await self.get(project_id)
        if c.direction is None or c.script is None:
            raise ScriptError("сначала нужен сценарий видео")
        brief = await self.brief(c.project.product_id)
        instruction = creative.voiceover_instruction(kind, c.voiceover, text)
        async with self.products.spend(c.project.product_id, "voiceover"):
            vo = await creative.write_voiceover(self.llm(), brief, c.direction, c.script,
                                                self.chars_per_sec(), instruction=instruction)
        await self._update(project_id, voiceover=vo.model_dump())
        return vo

    # ---------- оператор: промпты и непрерывность ----------

    async def make_shot_plans(self, project_id: int, kind: str = "new",
                              text: str = "") -> ShotPlans:
        c = await self.get(project_id)
        if c.direction is None or c.script is None:
            raise ScriptError("сначала нужен сценарий видео")
        brief = await self.brief(c.project.product_id)
        instruction = shots.plans_instruction(kind, c.shot_plan, text)
        async with self.products.spend(c.project.product_id, "shots"):
            plans = await shots.plan_shots(self.llm(), brief, c.direction, c.script,
                                           self.model(), instruction=instruction)
        await self._update(project_id, shot_plan=plans.model_dump())
        return plans


def _photo_desc(meta: dict, width: int, height: int) -> str:
    v = meta.get("vision") or {}
    desc = v.get("description") or "фото товара"
    quality = v.get("quality")
    extra = f", качество {quality}" if quality else ""
    return f"{desc} ({width}×{height}{extra})"
