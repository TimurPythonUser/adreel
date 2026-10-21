"""Страница товара по ссылке: текст, название, цена, картинки.

Порядок как в исходном ТЗ: JSON-LD Product (schema.org) → OpenGraph/meta → текст
страницы через trafilatura. Что не нашли разметкой — вытащит LLM из текста.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import re
import socket
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from lxml import html as lxml_html

from .. import net

log = logging.getLogger("adreel.ingest.pages")

URL_RE = re.compile(r"https?://[^\s<>\"']+")

# Многие магазины отдают 403 на «голый» User-Agent — нужен полный браузерный набор
# заголовков (урок из AI_Host, bot/ingest.py).
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/128.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
              "image/webp,*/*;q=0.8",
    "Accept-Language": "uk-UA,uk;q=0.9,ru;q=0.8,en;q=0.7",
}

MAX_HTML_BYTES = 5 * 1024 * 1024
MAX_PAGE_IMAGES = 8
MAX_REDIRECTS = 5
REDIRECT_CODES = (301, 302, 303, 307, 308)

#: видео по ссылке качаем только отсюда: проверено, yt-dlp берёт без логина
VIDEO_HOSTS = ("youtube.com", "youtu.be")
#: соцсети отдают видео только залогиненным (Instagram проверен 15.09.2026: метаданные
#: есть, самого видео нет) — не пытаемся, сразу просим прислать файлом
SOCIAL_HOSTS = ("instagram.com", "tiktok.com", "facebook.com", "fb.watch", "vimeo.com",
                "threads.net")
#: меньше этого текста без названия — страница собирается скриптами, нам в ней пусто
MIN_PAGE_TEXT = 200


class PageError(RuntimeError):
    pass


class PageBlocked(PageError):
    """Сайт не пускает ботов (403/401/429, защита Cloudflare и т.п.).

    Проверено 15.09.2026: так отвечают EVA и Comfy — крупные магазины часто закрыты.
    """


class PageEmpty(PageError):
    """Страница открылась, но товар дорисовывается скриптами — текста для нас нет."""


@dataclass
class PageData:
    url: str
    title: str = ""
    description: str = ""
    text: str = ""
    price: str = ""
    brand: str = ""
    images: list[str] = field(default_factory=list)
    jsonld: dict[str, Any] | None = None


def find_urls(text: str) -> list[str]:
    return [u.rstrip(".,;:)»]") for u in URL_RE.findall(text or "")]


def strip_urls(text: str) -> str:
    return URL_RE.sub(" ", text or "").strip()


def _host_in(url: str, hosts: tuple[str, ...]) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in hosts)


def is_video_url(url: str) -> bool:
    return _host_in(url, VIDEO_HOSTS)


def is_social_url(url: str) -> bool:
    return _host_in(url, SOCIAL_HOSTS)


def host_of(url: str) -> str:
    return (urlparse(url).hostname or url).removeprefix("www.")


async def check_public_url(url: str) -> None:
    """Только http(s) и только публичные адреса.

    Бот личный, но ссылки приходят из чужих сообщений: ссылка на 127.0.0.1 или
    169.254.169.254 (метаданные облака) не должна достать внутренние сервисы.
    """
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise PageError("нужна ссылка http(s)")
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, p.hostname, None)
    except socket.gaierror as exc:
        raise PageError(f"сайт {p.hostname} не найден") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise PageError("ссылка ведёт во внутреннюю сеть — не открываю")


async def fetch(url: str, *, max_bytes: int, who: str, accept: str | None = None,
                client: httpx.AsyncClient | None = None) -> httpx.Response:
    """GET чужого адреса: проверяем КАЖДЫЙ шаг редиректов, тело режем по max_bytes.

    follow_redirects у httpx не годится: промежуточный редирект на 127.0.0.1 или
    169.254.169.254 был бы уже выполнен, а проверить мы успели бы только финальный адрес.
    Остаётся DNS-rebinding между проверкой и соединением — для личного бота принимаем.
    """
    headers = dict(BROWSER_HEADERS)
    if accept:
        headers["Accept"] = accept
    for _ in range(MAX_REDIRECTS + 1):
        await check_public_url(url)
        try:
            r = await net.request("GET", url, who=who, timeout=30, follow_redirects=False,
                                  headers=headers, pauses=(2, 5), max_bytes=max_bytes,
                                  client=client)
        except net.TooLarge as exc:
            raise PageError(str(exc)) from exc
        location = r.headers.get("location")
        if r.status_code not in REDIRECT_CODES or not location:
            break
        url = urljoin(str(r.url), location)
    else:
        raise PageError(f"{who}: слишком много переадресаций")
    if r.status_code in (401, 403, 429):
        raise PageBlocked(f"{who}: HTTP {r.status_code}")
    if r.status_code >= 400:
        raise PageError(f"{who}: HTTP {r.status_code}")
    return r


async def fetch_page(url: str) -> PageData:
    r = await fetch(url, max_bytes=MAX_HTML_BYTES, who="страница товара")
    ctype = r.headers.get("content-type", "")
    if "html" not in ctype and "xml" not in ctype:
        raise PageError(f"по ссылке не страница, а {ctype or 'неизвестно что'}")
    return ensure_content(parse_page(str(r.url), r.text))


def ensure_content(page: PageData) -> PageData:
    """Пустая для нас страница — ошибка, а не «успех без данных».

    Проверено 15.09.2026: makeup.com.ua открывается с кодом 200, но без названия
    и с парой строк текста — товар дорисовывает JavaScript.
    """
    if not page.title and len(page.text) < MIN_PAGE_TEXT and not page.price:
        raise PageEmpty("на странице нет текста о товаре")
    return page


def parse_page(url: str, html: str) -> PageData:
    page = PageData(url=url)
    try:
        doc = lxml_html.fromstring(html)
    except (ValueError, lxml_html.etree.ParserError):
        doc = None

    if doc is not None:
        product = _jsonld_product(doc)
        if product:
            page.jsonld = product
            page.title = _s(product.get("name"))
            page.description = _s(product.get("description"))
            page.brand = _brand(product.get("brand"))
            page.price = _price(product.get("offers"))
            page.images += _images(product.get("image"))
        meta = _meta(doc)
        page.title = page.title or meta.get("og:title", "") or _title(doc)
        page.description = page.description or meta.get("og:description", "") \
            or meta.get("description", "")
        if not page.price and meta.get("product:price:amount"):
            page.price = f"{meta['product:price:amount']} {meta.get('product:price:currency', '')}"
        for key in ("og:image", "og:image:secure_url", "twitter:image"):
            if meta.get(key):
                page.images.append(meta[key])

    page.text = _article_text(html)
    seen: set[str] = set()
    images = []
    for src in page.images:
        full = urljoin(url, src)
        if full.startswith("http") and full not in seen:
            seen.add(full)
            images.append(full)
    page.images = images[:MAX_PAGE_IMAGES]
    page.price = page.price.strip()
    return page


def _s(v: Any) -> str:
    return v.strip() if isinstance(v, str) else ""


def _jsonld_product(doc: Any) -> dict[str, Any] | None:
    for node in doc.xpath('//script[@type="application/ld+json"]/text()'):
        try:
            data = json.loads(node)
        except (ValueError, TypeError):
            continue
        found = _find_product(data)
        if found:
            return found
    return None


def _find_product(data: Any) -> dict[str, Any] | None:
    if isinstance(data, list):
        for item in data:
            if (f := _find_product(item)) is not None:
                return f
    elif isinstance(data, dict):
        t = data.get("@type")
        types = t if isinstance(t, list) else [t]
        if "Product" in types or "ProductGroup" in types:
            return data
        for key in ("@graph", "mainEntity", "itemListElement"):
            if key in data and (f := _find_product(data[key])) is not None:
                return f
    return None


def _brand(v: Any) -> str:
    if isinstance(v, dict):
        return _s(v.get("name"))
    return _s(v)


def _price(offers: Any) -> str:
    if isinstance(offers, list):
        offers = offers[0] if offers else None
    if not isinstance(offers, dict):
        return ""
    amount = offers.get("price") or offers.get("lowPrice")
    if amount in (None, ""):
        spec = offers.get("priceSpecification")
        if isinstance(spec, dict):
            amount = spec.get("price")
    cur = offers.get("priceCurrency", "")
    return f"{amount} {cur}".strip() if amount not in (None, "") else ""


def _images(v: Any) -> list[str]:
    if isinstance(v, str):
        return [v]
    if isinstance(v, dict):
        return [u] if (u := _s(v.get("url") or v.get("contentUrl"))) else []
    if isinstance(v, list):
        return [u for item in v for u in _images(item)]
    return []


def _meta(doc: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in doc.xpath("//meta"):
        key = (m.get("property") or m.get("name") or "").strip().lower()
        val = (m.get("content") or "").strip()
        if key and val and key not in out:
            out[key] = val
    return out


def _title(doc: Any) -> str:
    t = doc.xpath("//title/text()")
    return t[0].strip() if t else ""


def _article_text(html: str) -> str:
    try:
        import trafilatura

        text = trafilatura.extract(html, include_comments=False, include_tables=True,
                                   favor_recall=True)
        if text:
            return text.strip()
    except Exception as exc:
        log.warning("trafilatura упал: %s", exc)
    body = re.sub(r"(?is)<(script|style|nav|header|footer|aside)[^>]*>.*?</\1>", " ", html)
    body = re.sub(r"(?s)<[^>]+>", " ", body)
    return re.sub(r"\s{2,}", " ", body).strip()


def page_as_text(page: PageData) -> str:
    """Текст материала для LLM: сначала разметка (надёжнее), потом текст страницы."""
    head = [f"Название: {page.title}" if page.title else "",
            f"Бренд: {page.brand}" if page.brand else "",
            f"Цена: {page.price}" if page.price else "",
            f"Описание: {page.description}" if page.description else ""]
    lines = [h for h in head if h]
    if page.text:
        lines.append(page.text)
    return "\n".join(lines)
