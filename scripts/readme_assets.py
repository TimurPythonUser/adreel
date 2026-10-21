"""Картинки для README: кадры и GIF из готового ролика, макеты экранов бота, карта трека.

Честно о том, что на картинках:
  • GIF и кадры — из настоящего ролика, который собрал AdReel (путь к mp4 — аргумент);
  • тексты и кнопки на макетах — настоящий вывод adreel/bot/view.py. Озвучка — дословно
    из субтитров ролика, сцены — по его склейкам, карточка — по тому, что видно
    на упаковке. Направления и трек — демонстрационные;
  • карта трека — настоящий разбор music/analyze.py синтетического трека.

    uv run python scripts/readme_assets.py путь/к/ролику.mp4

GIF и кадры пишутся в docs/img/. HTML-макеты складываются в build/readme/: их снимает
браузер (элемент .shot, ширина 880 px) в docs/img/*.png.
"""

from __future__ import annotations

import asyncio
import base64
import io
import math
import sys
from collections.abc import Callable
from html import escape
from pathlib import Path

import numpy as np
from aiogram.types import InlineKeyboardMarkup
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from adreel.bot import view  # noqa: E402
from adreel.db.models import Track  # noqa: E402
from adreel.gen.models import image_model, video_model  # noqa: E402
from adreel.gen.pipeline import Estimate  # noqa: E402
from adreel.music.analyze import SR, MusicMap, analyze, script_windows  # noqa: E402
from adreel.music.service import Choice  # noqa: E402
from adreel.product.schema import Fact, ProductCard  # noqa: E402
from adreel.render import ffmpeg  # noqa: E402
from adreel.script.schema import Direction, VideoScript, Voiceover  # noqa: E402

IMG = ROOT / "docs" / "img"
HTML = ROOT / "build" / "readme"

# ---------- товар из готового ролика ----------

CARD = ProductCard(
    language="uk", name="Head & Shoulders Deep Hydration", brand="Head & Shoulders",
    category="cosmetics", what_it_is="Зволожувальний шампунь проти лупи",
    audience="ті, кого дістала лупа",
    benefits=[Fact(text="до 100% без лупи*", source="photo:1"),
              Fact(text="клінічно перевірено", source="photo:1"),
              Fact(text="зволожує шкіру голови", source="photo:1")],
    price="242 грн", forbidden_claims=["лікує лупу назавжди"],
    tone="впевнений, трохи іронії", cta="Замовляй на сайті",
    answered=["offer", "forbidden_claims"],
)
DOUBTS = ["на этикетке «до 100%» со звёздочкой — условие из мелкого шрифта стоит проверить"]

DIRECTIONS = [
    Direction(title="Снег летом", format="метафора → решение",
              hook="Посреди тёмного кадра на флакон сыплется «снег» — как перхоть на плечах",
              idea="Перхоть показываем метафорой: снег, которого не ждёшь. Свет гаснет — "
                   "проблема возвращается. Потом флакон в лучах: до 100% без перхоти.",
              visual_style="тёмный фон, контровой свет, медленные облёты, снежинки",
              voice_tone="уверенный, с лёгкой иронией",
              music_mood="тёмный бит с дропом на раскрытии",
              why_it_works="метафору понимают с первой секунды, даже без звука",
              risks="снежинки у видеомодели могут липнуть к флакону — держим их в воздухе"),
    Direction(title="Чёрная футболка", format="до / после",
              hook="Крупно плечо в чёрной футболке, на ткани белые хлопья",
              idea="Утро, тёмная одежда, неловкость. Через неделю — та же футболка, чистая.",
              visual_style="дневной свет, крупные планы ткани, спокойный монтаж",
              voice_tone="дружеский", music_mood="лёгкий поп",
              why_it_works="боль узнают мгновенно",
              risks="«после» нельзя обещать как гарантию — только как иллюстрацию"),
    Direction(title="Клинически проверено", format="демонстрация",
              hook="Лабораторный свет, капля шампуня в макро",
              idea="Сухо и убедительно: макро текстуры, «клинически проверено», цена в конце.",
              visual_style="белый фон, макро, склейки в такт", voice_tone="спокойный экспертный",
              music_mood="минимал-электроника", why_it_works="доверие к доказанному эффекту",
              risks="мелкий текст этикетки модель исказит — даём его оверлеем"),
]


def shot(n: int, dur: float, role: str, visual: str, camera: str, source: str) -> dict:
    return {"n": n, "duration_sec": dur, "role": role, "visual": visual, "camera": camera,
            "source": source, "photo_ref": 1}


#: сцены — по склейкам ролика (3.4 / 7.0 / 11.9 с)
SCRIPT = VideoScript.model_validate({"title": "Сніг улітку", "key_shot": 3, "shots": [
    shot(1, 3.5, "hook", "Флакон в темноте, сверху сыплются снежинки — как перхоть",
         "медленный наезд, снег летит в кадр", "generated_scene"),
    shot(2, 3.5, "problem", "Свет гаснет, флакон уходит в тень — проблема возвращается",
         "лёгкий отъезд, свет тускнеет", "generated_scene"),
    shot(3, 4.9, "solution", "Флакон в контровом свете, капли воды на этикетке",
         "облёт по дуге вокруг флакона", "product_photo"),
    shot(4, 3.1, "cta", "Флакон на тёмном столе в мягком свете — финальная карточка",
         "статичный кадр, лёгкий зум", "end_card"),
]})

#: дословно из субтитров ролика
VOICE = Voiceover.model_validate({"lines": [
    {"n": 1, "text": "Лупа — це як сніг улітку. Не до речі."},
    {"n": 2, "text": "Знову і знову. Ти вже втомився це терпіти."},
    {"n": 3, "text": "До ста відсотків проти лупи — на упаковці. Клінічно перевірено."},
    {"n": 4, "text": "Двісті сорок дві гривні — і прощавай, лупа."},
]})

TRACK_TITLE = "Нічний біт"

# ---------- синтетический трек для карты ----------


def synth_track(bpm: float = 120, seconds: float = 80, sr: int = SR) -> np.ndarray:
    """Тихое вступление → нарастание → дроп на 0:28 → спад → второй дроп на 1:00."""
    beat = 60 / bpm
    n = int(seconds * sr)
    y = np.zeros(n, np.float32)
    rng = np.random.default_rng(7)

    def env(sec: float, decay: float) -> tuple[np.ndarray, np.ndarray]:
        t = np.arange(int(sec * sr)) / sr
        return t, np.exp(-t * decay)

    t, e = env(0.3, 11)
    kick = np.sin(2 * np.pi * (48 + 70 * np.exp(-t * 30)) * t) * e
    t, e = env(0.05, 80)
    hat = rng.standard_normal(t.size) * e
    t, e = env(0.14, 28)
    clap = rng.standard_normal(t.size) * e
    t, e = env(beat * 0.9, 3)
    bass = np.sin(2 * np.pi * 55 * t) * e

    def add(at: float, sig: np.ndarray, gain: float) -> None:
        i = int(at * sr)
        j = min(n, i + sig.size)
        if i < n:
            y[i:j] += gain * sig[: j - i]

    sections = [(0, 16, "intro"), (16, 28, "build"), (28, 48, "drop"), (48, 60, "break"),
                (60, 80, "drop")]
    for start, end, kind in sections:
        k = round(start / beat)
        while k * beat < end - 1e-6:
            b, accent = k * beat, 1.4 if k % 4 == 0 else 1.0
            if kind == "intro":
                add(b, kick, 0.16 * accent)
            elif kind == "build":
                p = (b - start) / (end - start)
                add(b, kick, (0.2 + 0.18 * p) * accent)
                add(b + beat / 2, hat, 0.03 + 0.09 * p)
            elif kind == "drop":
                add(b, kick, 0.75 * accent)
                add(b, bass, 0.3)
                add(b + beat / 2, hat, 0.12)
                if k % 2:
                    add(b, clap, 0.22)
            else:
                add(b, kick, 0.14 * accent)
                add(b + beat / 2, hat, 0.03)
            k += 1
    y += 0.015 * np.sin(2 * np.pi * 220 * np.arange(n) / sr).astype(np.float32)
    return (y / max(1e-6, float(np.abs(y).max())) * 0.9).astype(np.float32)


# ---------- кадры и GIF из готового ролика ----------


async def to_gif(video: Path, dst: Path, width: int = 270, fps: int = 9) -> None:
    """~3 МБ на 15 с: GitHub показывает GIF как есть, тяжёлый грузится долго."""
    flt = f"fps={fps},scale={width}:-1:flags=lanczos"
    palette = dst.with_name("_palette.png")
    try:
        await ffmpeg.run(["-i", str(video), "-vf",
                          f"{flt},palettegen=max_colors=96:stats_mode=diff", str(palette)])
        await ffmpeg.run(["-i", str(video), "-i", str(palette), "-lavfi",
                          f"{flt}[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=3"
                          ":diff_mode=rectangle", "-loop", "0", str(dst)])
    finally:
        palette.unlink(missing_ok=True)


async def frame_at(video: Path, sec: float, width: int = 540) -> bytes:
    return await ffmpeg.run(["-ss", f"{sec:.2f}", "-i", str(video), "-frames:v", "1",
                             "-vf", f"scale={width}:-1", "-f", "image2", "-c:v", "mjpeg",
                             "-q:v", "3", "pipe:1"])


async def filmstrip(video: Path, dst: Path, at: tuple[float, ...]) -> None:
    """Раскадровка: по кадру на сцену, скруглённые углы, тёмный фон под GitHub."""
    w, gap, pad = 216, 14, 14
    frames = [Image.open(io.BytesIO(await frame_at(video, t, w))).convert("RGB") for t in at]
    h = frames[0].height
    strip = Image.new("RGB", (pad * 2 + len(frames) * w + (len(frames) - 1) * gap, h + pad * 2),
                      (14, 22, 33))
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), radius=18, fill=255)
    for i, f in enumerate(frames):
        strip.paste(f.resize((w, h)), (pad + i * (w + gap), pad), mask)
    strip.save(dst, optimize=True)


# ---------- HTML-макеты Telegram ----------

CSS = """
* { box-sizing: border-box; margin: 0; }
body { background: #0e1621; font-family: 'Segoe UI', Roboto, Arial, sans-serif; }
.shot { width: 880px; background: #0e1621; padding-bottom: 28px; }
.head { display: flex; align-items: center; gap: 22px; background: #17212b;
        padding: 22px 28px; }
.head img { width: 76px; height: 76px; border-radius: 50%; }
.head b { color: #fff; font-size: 30px; display: block; }
.head span { color: #708499; font-size: 24px; }
.row { display: flex; padding: 14px 28px 0; }
.row.out { justify-content: flex-end; }
.msg { max-width: 760px; border-radius: 22px; padding: 18px 24px 12px; color: #f5f5f5;
       font-size: 25px; line-height: 1.42; background: #182533; }
.out .msg { background: #2b5278; }
.msg i { color: #c9d4de; }
.msg .t { color: #6d7f8f; font-size: 19px; text-align: right; margin-top: 6px; }
.out .msg .t { color: #8fb3d6; }
.msg a { color: #6ab3f3; text-decoration: none; }
.kb { margin: 6px 28px 0; max-width: 760px; }
.kb .r { display: flex; gap: 6px; margin-top: 6px; }
.kb .b { flex: 1; background: #22303f; color: #fff; border-radius: 14px; padding: 15px 10px;
         text-align: center; font-size: 23px; font-weight: 600; }
.media { border-radius: 16px; width: 100%; display: block; margin: -6px 0 12px; }
.audio { display: flex; align-items: center; gap: 20px; margin-bottom: 12px; }
.play { width: 76px; height: 76px; border-radius: 50%; background: #5288c1; position: relative;
        flex: none; }
.play:after { content: ''; position: absolute; left: 30px; top: 22px; border-style: solid;
              border-width: 16px 0 16px 26px;
              border-color: transparent transparent transparent #fff; }
.wave { display: flex; align-items: center; gap: 3px; height: 44px; }
.wave i { display: block; width: 5px; background: #5288c1; border-radius: 3px; }
.audio small { color: #6d7f8f; font-size: 20px; }
.audio b { display: block; font-size: 24px; }
.file { display: flex; align-items: center; gap: 18px; }
.file .ic { width: 70px; height: 70px; border-radius: 50%; background: #4a7fb5;
            display: flex; align-items: center; justify-content: center; font-size: 32px; }
.file small { color: #a9c3dd; font-size: 20px; }
.dur { position: relative; }
.dur span { position: absolute; left: 14px; top: 8px; background: rgba(0,0,0,.55);
            color: #fff; font-size: 19px; border-radius: 10px; padding: 2px 10px; }
"""


def _tg(text: str) -> str:
    """HTML-разметка Telegram (b, i, сущности) почти совпадает с обычной — переносы."""
    return text.replace("\n", "<br>")


def keyboard(markup: InlineKeyboardMarkup) -> str:
    rows = "".join(
        '<div class="r">' + "".join(f'<div class="b">{escape(b.text)}</div>' for b in row)
        + "</div>" for row in markup.inline_keyboard)
    return f'<div class="kb">{rows}</div>'


def bubble(inner: str, *, out: bool = False, time: str = "10:00",
           kb: InlineKeyboardMarkup | None = None, raw: bool = False) -> str:
    body = inner if raw else _tg(inner)
    side = "out" if out else "in"
    msg = f'<div class="row {side}"><div class="msg">{body}<div class="t">{time}</div></div></div>'
    return msg + (keyboard(kb) if kb else "")


def page(name: str, *parts: str) -> None:
    head = ('<div class="head"><img src="../../docs/img/logo.png"><div><b>AdReel</b>'
            '<span>бот</span></div></div>')
    html = (f'<!doctype html><html><head><meta charset="utf-8"><style>{CSS}</style></head>'
            f'<body><div class="shot">{head}{"".join(parts)}</div></body></html>')
    (HTML / f"{name}.html").write_text(html, encoding="utf-8")


def data_uri(data: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


def logo_page() -> None:
    html = """<!doctype html><html><head><meta charset="utf-8"><style>
@font-face { font-family: eUkraine; src: url('../../assets/fonts/e-Ukraine-Bold.ttf'); }
* { margin: 0; } body { background: transparent; }
.logo { width: 512px; height: 512px; position: relative; overflow: hidden;
  background: linear-gradient(135deg, #ff7a59 0%, #e2366f 55%, #7b2ff7 100%);
  font-family: eUkraine, sans-serif; }
.dot { position: absolute; width: 14px; height: 14px; border-radius: 50%;
  background: rgba(255,255,255,.75); }
.icon { position: absolute; left: 50%; top: 62px; transform: translateX(-50%);
  font-size: 190px; line-height: 1; filter: drop-shadow(0 10px 14px rgba(0,0,0,.25)); }
.name { position: absolute; width: 100%; text-align: center; top: 292px; color: #fff;
  font-size: 104px; letter-spacing: -2px;
  text-shadow: 0 6px 0 rgba(40,0,40,.35), 0 0 2px #000; }
.ai { position: absolute; width: 100%; text-align: center; top: 398px; color: #7dffcf;
  font-size: 84px; text-shadow: 0 6px 0 rgba(40,0,40,.35); }
</style></head><body><div class="logo">
<div class="dot" style="left:70px;top:90px"></div>
<div class="dot" style="left:420px;top:70px"></div>
<div class="dot" style="left:440px;top:250px;width:9px;height:9px"></div>
<div class="dot" style="left:52px;top:300px;width:9px;height:9px"></div>
<div class="icon">🎬</div><div class="name">AdReel</div><div class="ai">AI</div>
</div></body></html>"""
    (HTML / "logo.html").write_text(html, encoding="utf-8")


def music_page(mmap: MusicMap, w_start: float, w_end: float) -> None:
    """Карта трека: сверху весь трек и где взят кусок, снизу кусок крупно — доли,
    дроп и сцены сценария поверх. Кусок 15 с на 80-секундном треке иначе не разглядеть."""
    width, left, right = 880, 40, 840
    o_top, o_base = 112, 222            # обзор всего трека
    z_top, z_base = 300, 470            # кусок под ролик крупно
    height = 600
    energy = mmap.energy

    def area(t0: float, t1: float, top: float, base: float) -> tuple[str, Callable]:
        def x(t: float) -> float:
            return left + (right - left) * (t - t0) / (t1 - t0)

        pts = [(x(i * 0.5), base - e * (base - top)) for i, e in enumerate(energy)
               if t0 <= i * 0.5 <= t1]
        path = (f"M{pts[0][0]:.1f},{base} " + " ".join(f"L{a:.1f},{b:.1f}" for a, b in pts)
                + f" L{pts[-1][0]:.1f},{base} Z")
        return (f'<path d="{path}" fill="rgba(82,136,193,.55)" stroke="#6ab3f3" '
                'stroke-width="2"/>'), x

    o_area, ox = area(0, mmap.duration, o_top, o_base)
    z_area, zx = area(w_start, w_end, z_top, z_base)
    svg: list[str] = [o_area]
    for d in mmap.drops[:2]:
        svg.append(f'<line x1="{ox(d):.1f}" y1="{o_top - 6}" x2="{ox(d):.1f}" y2="{o_base}" '
                   'stroke="#ff5c7a" stroke-width="2" stroke-dasharray="6 5"/>'
                   f'<text x="{ox(d) + 7:.1f}" y="{o_base - 8}" fill="#ff5c7a" font-size="18" '
                   f'font-weight="700">дроп {view.mmss(d)}</text>')
    svg.append(f'<rect x="{ox(w_start):.1f}" y="{o_top - 10}" '
               f'width="{ox(w_end) - ox(w_start):.1f}" height="{o_base - o_top + 10}" rx="6" '
               'fill="rgba(255,122,89,.18)" stroke="#ff7a59" stroke-width="2"/>')
    # «лупа»: от куска на обзоре к крупному плану
    svg.append(f'<path d="M{ox(w_start):.1f},{o_base} L{left},{z_top - 18} '
               f'M{ox(w_end):.1f},{o_base} L{right},{z_top - 18}" stroke="#ff7a59" '
               'stroke-width="1.5" stroke-dasharray="4 5" fill="none"/>')
    svg.append(f'<rect x="{left}" y="{z_top - 18}" width="{right - left}" '
               f'height="{z_base - z_top + 118}" rx="12" fill="rgba(255,122,89,.07)" '
               'stroke="#ff7a59" stroke-width="2"/>')
    svg.append(z_area)
    svg += [f'<line x1="{zx(b):.1f}" y1="{z_base + 6}" x2="{zx(b):.1f}" y2="{z_base + 16}" '
            'stroke="#6d7f8f" stroke-width="2"/>' for b in mmap.beats if w_start <= b <= w_end]
    svg += [f'<line x1="{zx(b):.1f}" y1="{z_base + 6}" x2="{zx(b):.1f}" y2="{z_base + 28}" '
            'stroke="#e6edf3" stroke-width="3"/>' for b in mmap.downbeats
            if w_start <= b <= w_end]
    for d in mmap.drops:
        if w_start <= d <= w_end:
            svg.append(f'<line x1="{zx(d):.1f}" y1="{z_top - 10}" x2="{zx(d):.1f}" '
                       f'y2="{z_base + 28}" stroke="#ff5c7a" stroke-width="3" '
                       f'stroke-dasharray="8 6"/><text x="{zx(d) + 10:.1f}" y="{z_top + 14}" '
                       'fill="#ff5c7a" font-size="20" font-weight="700">💥 дроп → главный кадр'
                       '</text>')
    roles = {"hook": "хук", "problem": "проблема", "solution": "решение", "benefit": "выгода",
             "cta": "призыв"}
    t = w_start
    for sh in SCRIPT.shots:
        x0, x1 = zx(t), zx(t + sh.duration_sec)
        key = sh.n == SCRIPT.key_shot
        svg.append(f'<rect x="{x0 + 2:.1f}" y="{z_base + 40}" width="{x1 - x0 - 4:.1f}" '
                   f'height="52" rx="10" fill="{"#ff7a59" if key else "#2b5278"}"/>'
                   f'<text x="{(x0 + x1) / 2:.1f}" y="{z_base + 73}" fill="#fff" '
                   'font-size="20" text-anchor="middle" font-weight="700">'
                   f'{sh.n} · {roles[sh.role]}{" ⭐" if key else ""}</text>')
        t += sh.duration_sec
    doc = f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}"
font-family="Segoe UI, Roboto, Arial">
<rect width="{width}" height="{height}" fill="#0e1621"/>
<text x="{left}" y="46" fill="#fff" font-size="30" font-weight="700">Как бот слышит трек</text>
<text x="{left}" y="82" fill="#8fa3b5" font-size="21">темп {mmap.bpm:.0f} BPM · дропы
{", ".join(view.mmss(d) for d in mmap.drops[:2])} · под ролик взят кусок
{view.mmss(w_start)}–{view.mmss(w_end)}</text>
{"".join(svg)}
<text x="{left}" y="{height - 12}" fill="#6d7f8f" font-size="17">громкость по секундам ·
штрихи — доли, длинные — сильные доли (на них склейки)</text>
</svg>"""
    (HTML / "music-map.html").write_text(
        f'<!doctype html><html><head><meta charset="utf-8"><style>*{{margin:0}}'
        f'.shot{{width:{width}px}}</style></head><body><div class="shot">{doc}</div>'
        '</body></html>', encoding="utf-8")


def wave_html(n: int = 46, seed: int = 4) -> str:
    rng = np.random.default_rng(seed)
    bars = "".join(f'<i style="height:{int(8 + 34 * abs(math.sin(i / 3)) * rng.random())}px">'
                   "</i>" for i in range(n))
    return f'<div class="wave">{bars}</div>'


def chat_pages(mmap: MusicMap, choice: Choice, key_at: float, thumb: bytes) -> None:
    counts = view.counts_line({"link": 1, "text": 1})
    page("screen-card",
         bubble("<a>https://shop.example/head-shoulders-deep-hydration</a>", out=True,
                time="09:58", raw=True),
         bubble("Зроби щось незвичне про лупу. Можна з образом зими ❄️", out=True,
                time="09:58"),
         bubble(f"📥 Есть: {counts}.\nКидай ещё или жми «Готово».", time="09:58"),
         bubble(view.card_text(CARD, DOUBTS) + "\n\nВсё верно?", kb=view.review_kb(),
                time="09:59"))

    page("screen-directions",
         bubble(view.directions_text(DIRECTIONS), kb=view.directions_kb(DIRECTIONS)))

    drops = ", ".join(view.mmss(d) for d in mmap.drops[:3])
    length = round(choice.window.end - choice.window.start)
    file_html = ('<div class="file"><div class="ic">♪</div><div><b>nichnyi-bit.mp3</b><br>'
                 '<small>3.1 МБ</small></div></div>')
    player = (f'<div class="audio"><div class="play"></div><div><b>{escape(TRACK_TITLE)} · '
              f'кусок для ролика</b>{wave_html()}<small>0:{length:02d} · AdReel</small>'
              '</div></div>')
    page("screen-music",
         bubble(file_html, out=True, time="10:04", raw=True),
         bubble(f"🎵 <b>{escape(TRACK_TITLE)}</b> — сохранил в «Мои треки».\n"
                f"Длительность {view.mmss(mmap.duration)} · темп {mmap.bpm:.0f} BPM · "
                f"дроп: {drops}", time="10:04"),
         bubble(player + _tg(view.preview_caption(choice, "подобрал сам", key_at)),
                kb=view.preview_kb(1), time="10:04", raw=True))

    parts = view.final_text(SCRIPT, VOICE, choice, 14.5)
    page("screen-final", bubble("\n\n".join(parts), kb=view.final_kb(), time="10:07"))

    vm, im = video_model(), image_model()
    seconds = sum(vm.clip_duration(s.duration_sec) for s in SCRIPT.shots)
    images = sum(1 for s in SCRIPT.shots if s.source == "generated_scene")
    usd = round(vm.price(seconds) + images * im.price_per_image, 2)
    est = Estimate(images, len(SCRIPT.shots), seconds, usd, 0, len(SCRIPT.shots))
    video = (f'<div class="dur"><img class="media" src="{data_uri(thumb, "image/jpeg")}">'
             f'<span>0:{round(SCRIPT.total):02d}</span></div>')
    page("screen-done",
         bubble("✅ Утвердить и генерировать", out=True, time="10:08"),
         bubble(f"🎬 Делаю ролик: {est.text}.\nОбычно это 3–5 минут, пришлю готовое видео.",
                time="10:08"),
         bubble(video + _tg(f"✅ Готовый ролик · {round(SCRIPT.total)} с · потрачено на "
                            f"товар ${usd + 0.09:.2f}"), kb=view.done_kb(), time="10:13",
                raw=True))


async def main(video: Path) -> None:
    IMG.mkdir(parents=True, exist_ok=True)
    HTML.mkdir(parents=True, exist_ok=True)

    await to_gif(video, IMG / "demo.gif")
    await filmstrip(video, IMG / "frames.png", (0.8, 4.2, 8.6, 10.6, 13.4))
    thumb = await frame_at(video, 8.6, 520)

    mmap = analyze(synth_track())
    key_at = SCRIPT.start_of(SCRIPT.key_shot)
    window = script_windows(mmap, SCRIPT.total, key_at)[0]
    track = Track(owner_id=1, title=TRACK_TITLE, file_path="", sha256="",
                  duration=mmap.duration, bpm=mmap.bpm)
    choice = Choice(track, window, 0, round(SCRIPT.total), mmap)

    logo_page()
    music_page(mmap, window.start, window.end)
    chat_pages(mmap, choice, key_at, thumb)
    for p in (IMG / "demo.gif", IMG / "frames.png"):
        print(f"{p.name}: {p.stat().st_size // 1024} КБ")
    print("HTML:", ", ".join(sorted(p.name for p in HTML.glob("*.html"))))
    print(f"трек: {mmap.bpm} BPM, дропы {mmap.drops}, кусок {window.start}–{window.end}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: uv run python scripts/readme_assets.py путь/к/ролику.mp4")
    asyncio.run(main(Path(sys.argv[1])))
