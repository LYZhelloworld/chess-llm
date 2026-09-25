"""Unit tests for streaming chat support in LlmClient."""

from types import SimpleNamespace

import pytest

from src.agent.llm_client import LlmClient, ToolCall
from src.config import AppConfig, LlmProfile


def _chunk(content=None, reasoning=None, tool_calls=None, finish=None, model=None, usage=None):
    delta = SimpleNamespace(
        content=content,
        reasoning_content=reasoning,
        tool_calls=tool_calls,
    )
    choice = SimpleNamespace(delta=delta, finish_reason=finish)
    return SimpleNamespace(model=model, usage=usage, choices=[choice])


class _FakeCompletions:
    def __init__(self, chunks):
        self._chunks = chunks

    def create(self, **_kwargs):
        # The streaming branch passes stream=True; return an iterable of chunks.
        return iter(self._chunks)


class _FakeChat:
    def __init__(self, chunks):
        self.completions = _FakeCompletions(chunks)


class _FakeClient:
    def __init__(self, chunks):
        self.chat = _FakeChat(chunks)


def _make_client(stream_mode="auto"):
    profile = LlmProfile(name="test", api_key="sk-test", model="m", stream=stream_mode)
    client = LlmClient(profile)
    return client


def test_stream_reassembles_content_reasoning_and_tools():
    chunks = [
        _chunk(reasoning="Let me think", model="m"),
        _chunk(content="I will play"),
        _chunk(
            tool_calls=[
                SimpleNamespace(
                    index=0,
                    id="call_1",
                    function=SimpleNamespace(name="make_move", arguments='{"uci": "e2e4"}'),
                )
            ]
        ),
        _chunk(content=" e4.", finish="tool_calls"),
    ]
    client = _make_client()
    client._client = _FakeClient(chunks)

    deltas = []
    message = client.chat([], [], stream=True, on_delta=lambda c, r: deltas.append((c, r)))

    assert message.content == "I will play e4."
    assert message.reasoning == "Let me think"
    assert len(message.tool_calls) == 1
    assert isinstance(message.tool_calls[0], ToolCall)
    assert message.tool_calls[0].name == "make_move"
    assert message.tool_calls[0].arguments == '{"uci": "e2e4"}'
    # Deltas were emitted as they arrived.
    assert deltas[0] == ("", "Let me think")
    assert deltas[1] == ("I will play", "")


def test_stream_multiple_tool_calls_are_ordered():
    chunks = [
        _chunk(
            tool_calls=[
                SimpleNamespace(index=0, id="c0", function=SimpleNamespace(name="a", arguments="")),
                SimpleNamespace(index=1, id="c1", function=SimpleNamespace(name="b", arguments="")),
            ]
        ),
        _chunk(
            tool_calls=[
                SimpleNamespace(index=0, id="", function=SimpleNamespace(name="", arguments='{"x":1}')),
                SimpleNamespace(index=1, id="", function=SimpleNamespace(name="", arguments='{"y":2}')),
            ]
        ),
    ]
    client = _make_client()
    client._client = _FakeClient(chunks)

    message = client.chat([], [], stream=True)
    assert [c.name for c in message.tool_calls] == ["a", "b"]
    assert message.tool_calls[0].arguments == '{"x":1}'
    assert message.tool_calls[1].arguments == '{"y":2}'


def test_stream_reasoning_fragments_are_concatenated_without_extra_newlines():
    # Reasoning is streamed character/word by character/word. The reassembled
    # text must read as continuous prose, NOT one newline after every fragment.
    chunks = [
        _chunk(reasoning="The "),
        _chunk(reasoning="game "),
        _chunk(reasoning="is on."),
    ]
    client = _make_client()
    client._client = _FakeClient(chunks)

    message = client.chat([], [], stream=True)

    assert message.reasoning == "The game is on."
    assert "\n" not in message.reasoning


def test_stream_failure_falls_back_to_non_streaming():
    # First streaming attempt raises; the non-streaming path then succeeds.
    chunks = [_chunk(content="e4", finish="stop")]

    class _Hybrid:
        def __init__(self, good_chunks):
            self._chunks = good_chunks

        def create(self, **kwargs):
            if kwargs.get("stream"):
                raise RuntimeError("stream broke")
            # Non-streaming call returns a single response object.
            return SimpleNamespace(
                model="m",
                usage=None,
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content="e4", tool_calls=None, model_extra=None),
                        finish_reason="stop",
                    )
                ],
            )

    client = _make_client()
    client._client = SimpleNamespace(chat=SimpleNamespace(completions=_Hybrid(chunks)))

    message = client.chat([], [], stream=True, on_delta=lambda c, r: None)
    assert message.content == "e4"
    # auto mode remembered the failure and will not try streaming again.
    assert not client.streaming_enabled()
