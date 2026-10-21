"""Себестоимость ролика: считаем по факту, а не по прикидке.

Идея из AI_Host (videonews/costs.py), но цены теперь не в коде и не в .env,
а в config/pricing.yaml и config/video_models.yaml.

Каждый платный вызов пишет событие в журнал текущего проекта (contextvars: проекты
могут идти параллельно, и каждый должен считать своё). На этапе 1 журнал уедет в БД
(таблица cost_events) — формат события уже совпадает.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field

from .config import load_yaml

log = logging.getLogger("adreel.costs")

@dataclass(frozen=True)
class CostEvent:
    provider: str      # anthropic | deepseek | elevenlabs | fal
    operation: str     # имя модели или операции
    units: float
    unit_type: str     # tokens_in+out | chars | video_sec | image
    usd: float


@dataclass
class Ledger:
    job: str
    events: list[CostEvent] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, event: CostEvent) -> None:
        with self._lock:
            self.events.append(event)

    @property
    def total_usd(self) -> float:
        with self._lock:
            return sum(e.usd for e in self.events)

    def lines(self) -> list[tuple[str, float, str, float]]:
        """[(провайдер/операция, единиц, тип единиц, $)] — сгруппировано."""
        acc: dict[tuple[str, str, str], list[float]] = {}
        with self._lock:
            for e in self.events:
                cur = acc.setdefault((e.provider, e.operation, e.unit_type), [0.0, 0.0])
                cur[0] += e.units
                cur[1] += e.usd
        return [(f"{p} / {op}", u, ut, usd) for (p, op, ut), (u, usd) in sorted(acc.items())]


_current: contextvars.ContextVar[Ledger | None] = contextvars.ContextVar(
    "adreel_ledger", default=None
)


@contextlib.contextmanager
def job_scope(ledger: Ledger) -> Iterator[Ledger]:
    """Всё, что потратят вызовы внутри блока, запишется в этот журнал."""
    token = _current.set(ledger)
    try:
        yield ledger
    finally:
        _current.reset(token)


def record(provider: str, operation: str, units: float, unit_type: str, usd: float) -> None:
    ledger = _current.get()
    if ledger is not None:
        ledger.add(CostEvent(provider, operation, units, unit_type, usd))


# ---------- тарифы ----------

def llm_usd(model: str, tokens_in: int, tokens_out: int) -> float:
    """$ за вызов LLM. Модель без цены в pricing.yaml — ошибка: молча считать 0 нельзя."""
    prices = load_yaml("pricing.yaml").get("llm", {})
    if model not in prices:
        raise KeyError(f"нет цены для модели {model!r} в config/pricing.yaml (раздел llm)")
    p_in, p_out = prices[model]
    return tokens_in / 1e6 * float(p_in) + tokens_out / 1e6 * float(p_out)


def tts_usd(chars: int) -> float:
    return chars * float(load_yaml("pricing.yaml")["elevenlabs"]["usd_per_char"])


def record_llm(provider: str, model: str, tokens_in: int, tokens_out: int) -> None:
    try:
        usd = llm_usd(model, tokens_in, tokens_out)
    except KeyError as exc:
        # Вызов уже оплачен — ронять из-за справочника цен нельзя. Но и молчать тоже:
        # старт бота проверяет цены (health.preflight), сюда попадаем только при сбое.
        log.error("%s — вызов записан с $0", exc)
        usd = 0.0
    record(provider, model, tokens_in + tokens_out, "tokens", usd)


def format_report(ledger: Ledger) -> str:
    """Сообщение для Telegram (HTML)."""
    rows = ledger.lines()
    if not rows:
        return ""
    body = "\n".join(
        f"  • {name} — <i>{units:g} {unit_type}</i>: ${usd:.3f}"
        for name, units, unit_type, usd in rows
    )
    return f"💰 <b>Себестоимость</b>\n{body}\n\n<b>Итого: ${ledger.total_usd:.3f}</b>"
