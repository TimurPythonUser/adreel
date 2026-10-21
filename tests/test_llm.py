from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from adreel import costs
from adreel.llm.base import LLMError, image_block, text_block
from adreel.llm.claude import ClaudeLLM
from adreel.llm.deepseek import DeepSeekLLM
from adreel.llm.fake import FakeLLM
from adreel.llm.repair import parse_with_repair, strip_fence


class Shot(BaseModel):
    text: str
    seconds: int


def scripted(*answers: tuple[str, str | None]):
    calls: list[tuple[list[dict[str, str]], int]] = []
    queue = list(answers)

    async def call(messages, max_tokens):
        calls.append((list(messages), max_tokens))
        return queue.pop(0)

    return call, calls


async def test_repair_asks_again_with_validation_error():
    call, calls = scripted(('{"text": "hi"}', "stop"), ('{"text": "hi", "seconds": 5}', "stop"))
    shot = await parse_with_repair(call, system="s", user="u", output=Shot,
                                   max_tokens=100, who="t")
    assert shot == Shot(text="hi", seconds=5)
    second_messages = calls[1][0]
    assert second_messages[-1]["role"] == "user"
    assert "не прошёл проверку" in second_messages[-1]["content"]


async def test_repair_doubles_tokens_on_length_cutoff():
    call, calls = scripted(('{"text": "h', "length"), ('{"text": "h", "seconds": 1}', "stop"))
    await parse_with_repair(call, system="s", user="u", output=Shot, max_tokens=100, who="t")
    assert [c[1] for c in calls] == [100, 200]


async def test_repair_gives_up_after_attempts():
    call, _ = scripted(("nope", "stop"), ("nope", "stop"), ("nope", "stop"))
    with pytest.raises(LLMError, match="3 попытки"):
        await parse_with_repair(call, system="s", user="u", output=Shot, max_tokens=10, who="t")


def test_strip_fence():
    assert json.loads(strip_fence('```json\n{"a": 1}\n```')) == {"a": 1}


async def test_deepseek_rejects_images():
    llm = DeepSeekLLM("deepseek-v4-pro", "key")
    with pytest.raises(LLMError, match="картинки"):
        await llm.parse("s", [text_block("t"), image_block(b"x", "image/png")], Shot)


async def test_fake_llm_returns_in_order():
    llm = FakeLLM([{"text": "a", "seconds": 1}, Shot(text="b", seconds=2)])
    assert (await llm.parse("s", "u", Shot)).text == "a"
    assert (await llm.parse("s", "u", Shot)).text == "b"
    with pytest.raises(LLMError):
        await llm.parse("s", "u", Shot)


class _FakeMessages:
    def __init__(self, resp):
        self.resp = resp
        self.kwargs = None

    async def parse(self, **kwargs):
        self.kwargs = kwargs
        return self.resp


def _claude_with(resp):
    msgs = _FakeMessages(resp)
    return ClaudeLLM("claude-opus-5", "k", client=SimpleNamespace(messages=msgs)), msgs


async def test_claude_parse_records_cost_and_returns_model():
    resp = SimpleNamespace(stop_reason="end_turn", parsed_output=Shot(text="x", seconds=3),
                           usage=SimpleNamespace(input_tokens=1000, output_tokens=500))
    llm, msgs = _claude_with(resp)
    ledger = costs.Ledger("job")
    with costs.job_scope(ledger):
        out = await llm.parse("sys", "hi", Shot)
    assert out.seconds == 3
    assert msgs.kwargs["output_format"] is Shot
    assert ledger.total_usd == pytest.approx(1000 / 1e6 * 5 + 500 / 1e6 * 25)


@pytest.mark.parametrize("reason", ["refusal", "max_tokens"])
async def test_claude_bad_stop_reasons_raise(reason):
    resp = SimpleNamespace(stop_reason=reason, parsed_output=None,
                           usage=SimpleNamespace(input_tokens=1, output_tokens=1))
    llm, _ = _claude_with(resp)
    with pytest.raises(LLMError):
        await llm.parse("sys", "hi", Shot)
