from __future__ import annotations

import httpx
import pytest

from adreel.ingest import pages

JSONLD_HTML = """
<html><head>
<title>Термокружка — магазин</title>
<meta property="og:title" content="OG заголовок">
<meta property="og:image" content="/img/og.jpg">
<script type="application/ld+json">
{"@context": "https://schema.org", "@graph": [
  {"@type": "BreadcrumbList"},
  {"@type": "Product", "name": "Термокружка Steel 450 мл",
   "description": "Держит тепло до 8 часов",
   "brand": {"@type": "Brand", "name": "HotCup"},
   "image": ["https://shop.test/a.jpg", {"url": "https://shop.test/b.jpg"}],
   "offers": {"@type": "Offer", "price": "899", "priceCurrency": "UAH"}}
]}
</script>
</head><body><article><p>Термокружка из нержавеющей стали. Держит тепло до 8 часов,
холод до 12. Объём 450 мл, герметичная крышка.</p></article></body></html>
"""

OG_HTML = """
<html><head>
<meta property="og:title" content="Крем для рук">
<meta property="og:description" content="Питательный крем 75 мл">
<meta property="product:price:amount" content="249">
<meta property="product:price:currency" content="UAH">
<meta property="og:image" content="https://shop.test/cream.png">
</head><body></body></html>
"""


def test_jsonld_product_wins():
    page = pages.parse_page("https://shop.test/p/1", JSONLD_HTML)
    assert page.title == "Термокружка Steel 450 мл"
    assert page.brand == "HotCup"
    assert page.price == "899 UAH"
    assert page.images[:2] == ["https://shop.test/a.jpg", "https://shop.test/b.jpg"]
    assert "https://shop.test/img/og.jpg" in page.images     # относительный путь достроен
    assert page.jsonld is not None
    assert "Держит тепло" in pages.page_as_text(page)


def test_opengraph_fallback():
    page = pages.parse_page("https://shop.test/cream", OG_HTML)
    assert page.title == "Крем для рук"
    assert page.description == "Питательный крем 75 мл"
    assert page.price == "249 UAH"
    assert page.images == ["https://shop.test/cream.png"]


def test_urls_and_video_detection():
    text = "Глянь https://shop.test/p/1, и видео https://youtu.be/abc)."
    assert pages.find_urls(text) == ["https://shop.test/p/1", "https://youtu.be/abc"]
    assert pages.strip_urls(text).startswith("Глянь")
    assert pages.is_video_url("https://www.youtube.com/shorts/abc")
    assert not pages.is_video_url("https://www.tiktok.com/@x/video/1")
    assert pages.is_social_url("https://www.instagram.com/reel/abc")
    assert not pages.is_social_url("https://shop.test/p/1")
    assert pages.host_of("https://www.eva.ua/ua/pr1") == "eva.ua"


def test_empty_js_page_is_an_error_not_a_success():
    empty = pages.parse_page("https://shop.test/x", "<html><body><div id=app></div></body></html>")
    with pytest.raises(pages.PageEmpty):
        pages.ensure_content(empty)
    ok = pages.parse_page("https://shop.test/p/1", JSONLD_HTML)
    assert pages.ensure_content(ok) is ok


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/admin",
    "http://169.254.169.254/latest/meta-data",
    "http://10.0.0.5/",
    "file:///etc/passwd",
])
async def test_private_and_non_http_urls_blocked(url):
    with pytest.raises(pages.PageError):
        await pages.check_public_url(url)


#: публичный IP-литерал: getaddrinfo разбирает его без DNS, тест сеть не трогает
PUBLIC = "http://93.184.216.34"


def mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_redirect_into_internal_network_is_not_followed():
    """Промежуточный редирект на метаданные облака не должен быть выполнен вообще."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.host == "93.184.216.34":
            return httpx.Response(302, headers={"location": "http://169.254.169.254/meta"})
        return httpx.Response(200, text="секреты")

    async with mock_client(handler) as client:
        with pytest.raises(pages.PageError, match="внутреннюю сеть"):
            await pages.fetch(f"{PUBLIC}/p", max_bytes=1000, who="страница", client=client)
    assert seen == [f"{PUBLIC}/p"]


async def test_public_redirects_are_followed():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/old":
            return httpx.Response(301, headers={"location": "/new"})
        return httpx.Response(200, text="ок")

    async with mock_client(handler) as client:
        r = await pages.fetch(f"{PUBLIC}/old", max_bytes=1000, who="страница", client=client)
    assert r.text == "ок" and str(r.url) == f"{PUBLIC}/new"


async def test_oversized_body_is_cut_without_reading_it_all():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 5000)

    async with mock_client(handler) as client:
        with pytest.raises(pages.PageError, match="больше"):
            await pages.fetch(f"{PUBLIC}/big", max_bytes=1000, who="картинка", client=client)
