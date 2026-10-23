"""Каталог моделей генерации из config/video_models.yaml.

Сценарист получает отсюда ограничения модели (допустимые длительности клипа),
монтаж и оценка стоимости — цену. Код про конкретные модели ничего не знает.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from ..config import load_yaml, settings


class VideoModel(BaseModel):
    key: str
    title: str
    fal_id: str
    #: image — стартовый кадр + промпт; reference — референсы (фото товара, хвост клипа)
    mode: Literal["image", "reference"] = "image"
    durations: list[int] = Field(min_length=1)
    duration_as_string: bool = False
    supports_end_frame: bool = False
    supports_seed: bool = False
    price_per_sec: float
    inputs: dict[str, str]
    extra: dict[str, Any] = Field(default_factory=dict)
    output: str
    verified: bool = False
    # Как писать промпт именно для этой модели (синтаксис камеры, что она не умеет).
    # Уходит агенту-оператору (script/shots.py).
    prompt_guide: str = ""
    max_prompt_chars: int = 1500
    #: можно ли отдать последний кадр клипа стартовым в следующий (непрерывность)
    supports_chain: bool = False
    #: чем продолжать критичные стыки: ключ модели режима reference (хвост видео + фото)
    chain_model: str | None = None
    #: сколько секунд хвоста прошлого клипа уходит референсом (режим reference)
    reference_video_sec: float = 1.0
    #: надбавка за референсы к цене клипа — оценка с запасом, сверить живым прогоном
    reference_usd_per_call: float = 0.0

    @model_validator(mode="after")
    def _check(self) -> VideoModel:
        self.durations = sorted(set(self.durations))
        needed: tuple[str, ...] = ("prompt", "duration")
        needed += (("reference_images", "reference_videos") if self.mode == "reference"
                   else ("image",))
        for need in needed:
            if need not in self.inputs:
                raise ValueError(f"модель {self.key}: в inputs нет поля {need!r}")
        return self

    @property
    def max_sec(self) -> int:
        return self.durations[-1]

    def clip_duration(self, needed_sec: float) -> int:
        """Сколько секунд заказать у модели, чтобы хватило на needed_sec.

        Клип берём НЕ КОРОЧЕ нужного: лишнее срежет монтаж, а недостающее не
        доснимешь. Если нужно больше максимума — это ошибка сценария, а не повод
        молча обрезать сцену.
        """
        for d in self.durations:
            if d >= needed_sec - 1e-6:
                return d
        raise ValueError(
            f"{self.title}: клип {needed_sec:.1f} с длиннее максимума {self.max_sec} с"
        )

    def _common(self, prompt: str, duration: int, seed: int | None) -> dict[str, Any]:
        if duration not in self.durations:
            raise ValueError(f"{self.title}: недопустимая длительность {duration} с")
        args: dict[str, Any] = dict(self.extra)
        args[self.inputs["prompt"]] = prompt
        args[self.inputs["duration"]] = str(duration) if self.duration_as_string else duration
        # Сид держит характер картинки одинаковым между сценами и при повторе.
        if seed is not None and self.supports_seed and "seed" in self.inputs:
            args[self.inputs["seed"]] = seed
        return args

    def arguments(self, *, image_url: str, prompt: str, duration: int,
                  end_image_url: str | None = None, seed: int | None = None) -> dict[str, Any]:
        """Тело запроса к fal в терминах этой модели."""
        if self.mode != "image":
            raise ValueError(f"{self.title}: это модель режима {self.mode}")
        args = self._common(prompt, duration, seed)
        args[self.inputs["image"]] = image_url
        if end_image_url:
            if not (self.supports_end_frame and "end_image" in self.inputs):
                raise ValueError(f"{self.title}: не умеет конечный кадр")
            args[self.inputs["end_image"]] = end_image_url
        return args

    def reference_arguments(self, *, prompt: str, duration: int, image_urls: list[str],
                            video_urls: list[str], seed: int | None = None) -> dict[str, Any]:
        """Тело запроса для продолжения по референсам: фото товара + хвост прошлого клипа."""
        if self.mode != "reference":
            raise ValueError(f"{self.title}: это модель режима {self.mode}")
        args = self._common(prompt, duration, seed)
        args[self.inputs["reference_images"]] = image_urls
        args[self.inputs["reference_videos"]] = video_urls
        return args

    def price(self, duration: int, *, with_reference: bool = False) -> float:
        usd = duration * self.price_per_sec
        return usd + (self.reference_usd_per_call if with_reference else 0.0)


class ImageModel(BaseModel):
    key: str
    title: str
    fal_id: str
    #: edit — по референсам (фото товара, первая картинка героя); text — только по промпту,
    #: для сцен без товара и героя
    mode: Literal["edit", "text"] = "edit"
    price_per_image: float
    inputs: dict[str, str]
    extra: dict[str, Any] = Field(default_factory=dict)
    output: str
    verified: bool = False

    def arguments(self, *, image_urls: list[str], prompt: str,
                  aspect_ratio: str | None = None) -> dict[str, Any]:
        args: dict[str, Any] = dict(self.extra)
        if self.mode == "edit":
            args[self.inputs["images"]] = image_urls
        elif image_urls:
            raise ValueError(f"{self.title}: модель рисует только по тексту, референсы не берёт")
        args[self.inputs["prompt"]] = prompt
        if aspect_ratio and "aspect_ratio" in self.inputs:
            args[self.inputs["aspect_ratio"]] = aspect_ratio
        return args


def _section(kind: str) -> dict[str, Any]:
    data = load_yaml("video_models.yaml").get(kind) or {}
    if not data.get("models"):
        raise ValueError(f"config/video_models.yaml: пустой раздел {kind}")
    return data


def video_model(key: str | None = None) -> VideoModel:
    sec = _section("video")
    key = key or settings.video_model or sec["default"]
    if key not in sec["models"]:
        raise KeyError(f"видеомодели {key!r} нет в config/video_models.yaml")
    return VideoModel(key=key, **sec["models"][key])


def image_model(key: str | None = None) -> ImageModel:
    sec = _section("image")
    key = key or settings.image_model or sec["default"]
    if key not in sec["models"]:
        raise KeyError(f"модели картинок {key!r} нет в config/video_models.yaml")
    return ImageModel(key=key, **sec["models"][key])


def text_image_model() -> ImageModel | None:
    """Картинка только по тексту — стартовый кадр сцены без товара и без героя-референса.
    Не задана в конфиге — такие сцены рисует edit-модель от кадра героя или не рисует."""
    sec = _section("image")
    key = sec.get("text_default")
    if not key:
        return None
    if key not in sec["models"]:
        raise KeyError(f"модели картинок {key!r} нет в config/video_models.yaml")
    return ImageModel(key=key, **sec["models"][key])


def all_video_models() -> list[VideoModel]:
    return [VideoModel(key=k, **v) for k, v in _section("video")["models"].items()]


def pick(data: Any, path: str) -> Any:
    """Достать значение по пути вида "images.0.url" из ответа fal."""
    cur = data
    for part in path.split("."):
        if isinstance(cur, list):
            cur = cur[int(part)]
        elif isinstance(cur, dict):
            cur = cur[part]
        else:
            raise KeyError(path)
    return cur
