"""Шлюз fal: главное — не заплатить дважды за один и тот же вход."""

from __future__ import annotations

from types import SimpleNamespace

import fal_client
import httpx
import pytest

from adreel import costs, net
from adreel.gen.base import ImageInput
from adreel.gen.fal import FalGateway, FalVideoGen, GenerationFailed, GenerationTimeout
from adreel.gen.models import video_model
from adreel.gen.pending import PendingStore, fingerprint


class FakeFal:
    def __init__(self, statuses=None, result=None):
        self.statuses = list(statuses or [])
        self.result_value = result or {"video": {"url": "https://cdn/x.mp4"}}
        self.submitted: list[dict] = []
        self.uploads = 0

    async def submit(self, application, arguments):
        self.submitted.append(arguments)
        return SimpleNamespace(request_id=f"req-{len(self.submitted)}")

    async def status(self, application, request_id):
        if not self.statuses:
            return fal_client.Completed(logs=None, metrics={})
        st = self.statuses.pop(0)
        if isinstance(st, Exception):
            raise st
        return st

    async def result(self, application, request_id):
        return self.result_value

    async def upload(self, data, content_type):
        self.uploads += 1
        return "https://cdn/upload"


def gateway(fal, tmp_path, **kw):
    return FalGateway(fal, PendingStore(tmp_path / "pending.json"), poll_sec=0, **kw)


async def _args():
    return {"prompt": "p"}


async def test_submit_wait_result_and_forget(tmp_path):
    fal = FakeFal(statuses=[fal_client.Queued(position=1), fal_client.InProgress(logs=None)])
    gw = gateway(fal, tmp_path)
    res = await gw.run("m", "k1", _args)
    assert res["video"]["url"].endswith(".mp4")
    assert len(fal.submitted) == 1
    # файл ещё не скачан — запись держим, пока вызывающий не скажет done()
    assert await gw.store.get("k1") is not None
    await gw.done("k1")
    assert await gw.store.get("k1") is None


def http_error(code: int) -> fal_client.FalClientHTTPError:
    return fal_client.FalClientHTTPError("err", code, {}, httpx.Response(code))


async def test_transient_status_error_waits_for_old_request(tmp_path):
    """429/5xx на проверке старого запроса — не повод платить заново."""
    fal = FakeFal(statuses=[http_error(503), fal_client.InProgress(logs=None)])
    gw = gateway(fal, tmp_path)
    await gw.store.remember("k1", "m", "old-req")
    await gw.run("m", "k1", _args)
    assert fal.submitted == []


async def test_vanished_old_request_is_resubmitted(tmp_path):
    fal = FakeFal(statuses=[http_error(404)])
    gw = gateway(fal, tmp_path)
    await gw.store.remember("k1", "m", "old-req")
    await gw.run("m", "k1", _args)
    assert len(fal.submitted) == 1


async def test_failed_download_keeps_paid_request(tmp_path, monkeypatch):
    """Скачивание оплаченного клипа упало — повтор забирает тот же запрос, без оплаты."""
    fal = FakeFal()
    gw = gateway(fal, tmp_path)
    vg = FalVideoGen(gw, video_model("h3-max"))
    image = ImageInput(b"frame", "image/jpeg")

    async def broken(url, *, who="", **kw):
        raise net.ServiceUnavailable("обрыв")

    monkeypatch.setattr(net, "download", broken)
    with costs.job_scope(ledger := costs.Ledger("t")), pytest.raises(net.ServiceUnavailable):
        await vg.image_to_video(image, "p", 5, seed=1)
    assert len(fal.submitted) == 1 and not ledger.events

    async def ok(url, *, who="", **kw):
        return b"mp4"

    monkeypatch.setattr(net, "download", ok)
    with costs.job_scope(ledger):
        assert await vg.image_to_video(image, "p", 5, seed=1) == b"mp4"
    assert len(fal.submitted) == 1                  # подобрали старый запрос
    assert len(ledger.events) == 1                  # и записали стоимость один раз
    key = fingerprint(vg.model.fal_id, prompt="p", duration=5, seed=1, image=b"frame", end=None)
    assert await gw.store.get(key) is None


async def test_reuses_completed_request_without_paying_again(tmp_path):
    fal = FakeFal()
    gw = gateway(fal, tmp_path)
    await gw.store.remember("k1", "m", "old-req")
    said: list[str] = []

    async def progress(text):
        said.append(text)

    async def must_not_build():
        raise AssertionError("не должны загружать и отправлять заново")

    await gw.run("m", "k1", must_not_build, progress)
    assert fal.submitted == []
    assert "без оплаты" in said[0]


async def test_waits_for_old_in_progress_request(tmp_path):
    fal = FakeFal(statuses=[fal_client.InProgress(logs=None), fal_client.InProgress(logs=None)])
    gw = gateway(fal, tmp_path)
    await gw.store.remember("k1", "m", "old-req")
    await gw.run("m", "k1", _args)
    assert fal.submitted == []


async def test_failed_old_request_is_resubmitted(tmp_path):
    failed = fal_client.Completed(logs=None, metrics={}, error="boom", error_type="x")
    fal = FakeFal(statuses=[failed])
    gw = gateway(fal, tmp_path)
    await gw.store.remember("k1", "m", "old-req")
    await gw.run("m", "k1", _args)
    assert len(fal.submitted) == 1


async def test_model_error_raises_and_forgets(tmp_path):
    failed = fal_client.Completed(logs=None, metrics={}, error="nsfw", error_type="policy")
    fal = FakeFal(statuses=[failed])
    gw = gateway(fal, tmp_path)
    with pytest.raises(GenerationFailed, match="nsfw"):
        await gw.run("m", "k1", _args)
    assert await gw.store.get("k1") is None


async def test_timeout_keeps_record_for_next_attempt(tmp_path):
    # max_wait_sec=0: таймер на Windows грубый (~15 мс), дробный лимит был бы нестабилен
    fal = FakeFal(statuses=[fal_client.InProgress(logs=None)])
    gw = gateway(fal, tmp_path, max_wait_sec=0)
    with pytest.raises(GenerationTimeout):
        await gw.run("m", "k1", _args)
    assert (await gw.store.get("k1")) == {"model": "m", "request_id": "req-1"}


def test_fingerprint_depends_on_bytes_not_urls():
    a = fingerprint("m", image=b"photo", prompt="p")
    assert a == fingerprint("m", prompt="p", image=b"photo")
    assert a != fingerprint("m", image=b"photo2", prompt="p")
    assert a != fingerprint("other", image=b"photo", prompt="p")
