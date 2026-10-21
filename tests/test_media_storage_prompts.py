from __future__ import annotations

import io

import pytest
from aiogram.fsm.storage.base import StorageKey
from PIL import Image

from adreel import prompts
from adreel.bot.states import Flow
from adreel.bot.storage import JsonFileStorage
from adreel.gen.fake import solid_png
from adreel.ingest.media import MediaStore, NotAnImage, inspect_image


def test_media_store_dedupes_by_content(tmp_path):
    store = MediaStore(tmp_path)
    a = store.save(b"same", "image/png")
    b = store.save(b"same", "image/png")
    assert a == b
    assert store.read(a.rel_path) == b"same"
    assert a.rel_path.startswith("media/") and a.rel_path.endswith(".png")


def test_inspect_image_converts_exotic_formats_to_png():
    buf = io.BytesIO()
    Image.new("RGB", (10, 20), (255, 0, 0)).save(buf, "GIF")
    info = inspect_image(buf.getvalue())
    assert info.mime == "image/png" and (info.width, info.height) == (10, 20)
    assert inspect_image(solid_png(4, 4)).mime == "image/png"
    with pytest.raises(NotAnImage):
        inspect_image(b"<html>not an image</html>")


async def test_dialog_state_survives_restart_and_busy_is_revived(tmp_path):
    path = tmp_path / "dialogs.json"
    key = StorageKey(bot_id=1, chat_id=42, user_id=42)
    s1 = JsonFileStorage(path)
    await s1.set_state(key, Flow.busy)
    await s1.update_data(key, {"pid": 7})

    s2 = JsonFileStorage(path)
    assert await s2.get_data(key) == {"pid": 7}
    revived = s2.revive({Flow.busy.state or "": Flow.collecting.state or ""})
    assert revived == [(42, Flow.collecting.state)]
    assert await s2.get_state(key) == Flow.collecting.state


def test_broken_dialogs_file_does_not_crash(tmp_path):
    path = tmp_path / "dialogs.json"
    path.write_text("{битый", encoding="utf-8")
    assert JsonFileStorage(path)._cache == {}


@pytest.mark.parametrize("name", ["product_images", "product_card", "product_card_edit"])
def test_prompts_have_versions(name):
    assert prompts.load(name).version.startswith(name)


def test_render_requires_all_variables():
    with pytest.raises(KeyError, match="language"):
        prompts.render("product_images", first_index=1)
