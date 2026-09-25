"""OpenAI-compatible chat client with tool calling, retry and streaming."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from src.config import LlmProfile

LOGGER = logging.getLogger("src.agent.llm_client")

try:  # pragma: no cover - import guard so tests can stub the SDK
    from openai import OpenAI
    from openai import APIConnectionError, APIError, APITimeoutError, RateLimitError
except ImportError:  # pragma: no cover
    OpenAI = None  # type: ignore[assignment]

    class APIError(Exception):  # type: ignore[no-redef]
        pass

    class APITimeoutError(APIError):  # type: ignore[no-redef]
        pass

    class RateLimitError(APIError):  # type: ignore[no-redef]
        pass

    class APIConnectionError(APIError):  # type: ignore[no-redef]
        pass


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str
    raw: Dict[str, Any] = field(default_factory=dict)


# Reasoning models answer with the thinking in an extra field. The name is not
# standardised, so everything we know about is tried in order.
REASONING_FIELDS = ("reasoning_content", "reasoning", "thinking", "think")

# Key used when the reasoning is written into the transcript. It is stripped
# again before the history is sent back to a backend (see ChatMemory.window).
REASONING_KEY = "reasoning"


def extract_reasoning(message: Any) -> Optional[str]:
    """Pull the chain of thought out of a response message, if there is one."""

    for key in REASONING_FIELDS:
        value = None
        if isinstance(message, dict):
            value = message.get(key)
        else:
            value = getattr(message, key, None)
            if value is None:
                extra = getattr(message, "model_extra", None)
                if isinstance(extra, dict):
                    value = extra.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _extract_delta_reasoning(delta: Any) -> Optional[str]:
    """Same idea as :func:`extract_reasoning`, but for a streaming delta."""

    if delta is None:
        return None
    for key in REASONING_FIELDS:
        value = getattr(delta, key, None)
        if value is None:
            extra = getattr(delta, "model_extra", None)
            if isinstance(extra, dict):
                value = extra.get(key)
        if isinstance(value, str) and value:
            return value
    return None


@dataclass
class AssistantMessage:
    content: Optional[str]
    tool_calls: List[ToolCall]
    raw: Dict[str, Any] = field(default_factory=dict)
    reasoning: Optional[str] = None

    def to_message(self) -> Dict[str, Any]:
        """Serialize back to the OpenAI message format for the transcript."""

        message: Dict[str, Any] = {"role": "assistant"}
        if self.content:
            message["content"] = self.content
        else:
            message["content"] = ""
        if self.reasoning:
            message[REASONING_KEY] = self.reasoning
        if self.tool_calls:
            message["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": call.arguments},
                }
                for call in self.tool_calls
            ]
        return message


class LlmError(RuntimeError):
    """Raised when the backend could not be reached after all retries."""


class LlmClient:
    """Minimal wrapper over the OpenAI Chat Completions API."""

    def __init__(self, profile: LlmProfile) -> None:
        self.profile = profile
        if OpenAI is None:
            raise LlmError(
                "The 'openai' package is not installed. Run: pip install openai"
            )
        if not profile.resolved_api_key:
            LOGGER.warning(
                "No API key for profile %r (api_key empty and env %s unset); "
                "the backend may reject the call.",
                profile.name,
                profile.api_key_env,
            )
        self._client = OpenAI(
            base_url=profile.base_url,
            api_key=profile.resolved_api_key or "sk-no-key",
            timeout=profile.timeout,
            max_retries=0,  # retries are handled here so we can log them
        )
        # Set once a backend has shown it will not accept the chain of thought.
        self._reasoning_refused = False
        # Set once a backend has shown streaming is unavailable.
        self._stream_refused = False

    # --------------------------------------------------------------- reasoning
    def reasoning_enabled(self) -> bool:
        """Whether the stored chain of thought should be sent back."""

        mode = self.profile.reasoning_mode
        if mode == "off":
            return False
        if mode == "on":
            return True
        return not self._reasoning_refused

    def reasoning_refused(self) -> bool:
        """Record a refusal; returns True when a retry without it is worth it."""

        if self.profile.reasoning_mode != "auto" or self._reasoning_refused:
            return False
        self._reasoning_refused = True
        LOGGER.warning(
            "Backend %s refused the chain of thought; continuing without it.",
            self.profile.name,
        )
        return True

    # ----------------------------------------------------------------- streaming
    def streaming_enabled(self) -> bool:
        """Whether responses should be streamed token by token."""

        mode = self.profile.stream_mode
        if mode == "off":
            return False
        if mode == "on":
            return True
        return not self._stream_refused

    def _mark_stream_failed(self) -> bool:
        """Record a streaming failure; True only once (in "auto" mode)."""

        if self.profile.stream_mode != "auto" or self._stream_refused:
            return False
        self._stream_refused = True
        LOGGER.warning(
            "Streaming failed for %s; continuing without it.", self.profile.name
        )
        return True

    # ------------------------------------------------------------------ public
    def chat(
        self,
        messages: Sequence[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        *,
        stream: bool = False,
        on_delta: Optional[Callable[[str, str], None]] = None,
    ) -> AssistantMessage:
        """Send a chat request and return the assistant message.

        When ``stream`` is true (and the profile allows it) the reply is read
        token by token and each chunk of spoken text / chain of thought is
        handed to ``on_delta(content_delta, reasoning_delta)`` for live display.
        If streaming is unavailable or breaks, it falls back to the usual
        non-streaming call. Either way a complete :class:`AssistantMessage` is
        returned so the caller need not care which path was taken.
        """

        last_error: Optional[BaseException] = None
        attempts = max(1, self.profile.max_retries + 1)
        for attempt in range(1, attempts + 1):
            try:
                if stream and self.streaming_enabled():
                    try:
                        return self._chat_stream(messages, tools, on_delta)
                    except Exception as stream_exc:  # noqa: BLE001
                        # Retry once without streaming when auto mode allows it.
                        if self._mark_stream_failed():
                            LOGGER.warning(
                                "Streaming error (attempt %d/%d, model=%s): %s",
                                attempt,
                                attempts,
                                self.profile.model,
                                stream_exc,
                            )
                            return self._chat_once(messages, tools)
                        raise
                return self._chat_once(messages, tools)
            except (APITimeoutError, RateLimitError, APIConnectionError, APIError) as exc:
                last_error = exc
                LOGGER.warning(
                    "LLM call failed (attempt %d/%d, model=%s): %s",
                    attempt,
                    attempts,
                    self.profile.model,
                    exc,
                )
                if attempt < attempts:
                    time.sleep(self.profile.retry_backoff * attempt)
            except Exception as exc:  # noqa: BLE001 - network/SDK surprises
                last_error = exc
                LOGGER.warning(
                    "LLM call raised %s (attempt %d/%d): %s",
                    type(exc).__name__,
                    attempt,
                    attempts,
                    exc,
                )
                if attempt < attempts:
                    time.sleep(self.profile.retry_backoff * attempt)
        raise LlmError(f"LLM backend unavailable: {last_error}") from last_error

    # ----------------------------------------------------------------- private
    def _chat_once(
        self,
        messages: Sequence[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]],
    ) -> AssistantMessage:
        kwargs: Dict[str, Any] = {
            "model": self.profile.model,
            "messages": list(messages),
            "temperature": self.profile.temperature,
            "top_p": self.profile.top_p,
            "max_tokens": self.profile.max_tokens,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        response = self._client.chat.completions.create(**kwargs)
        choice = response.choices[0]
        message = choice.message

        tool_calls: List[ToolCall] = []
        for call in getattr(message, "tool_calls", None) or []:
            function = getattr(call, "function", None)
            tool_calls.append(
                ToolCall(
                    id=getattr(call, "id", "") or f"call_{len(tool_calls)}",
                    name=getattr(function, "name", "") if function else "",
                    arguments=getattr(function, "arguments", "") if function else "",
                )
            )

        raw = {
            "model": getattr(response, "model", self.profile.model),
            "finish_reason": getattr(choice, "finish_reason", None),
        }
        usage = getattr(response, "usage", None)
        if usage is not None:
            raw["usage"] = {
                "prompt_tokens": getattr(usage, "prompt_tokens", None),
                "completion_tokens": getattr(usage, "completion_tokens", None),
            }
        reasoning = extract_reasoning(message)
        if reasoning:
            LOGGER.debug("Captured %d characters of chain of thought.", len(reasoning))

        return AssistantMessage(
            content=message.content,
            tool_calls=tool_calls,
            raw=raw,
            reasoning=reasoning,
        )

    def _chat_stream(
        self,
        messages: Sequence[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]],
        on_delta: Optional[Callable[[str, str], None]],
    ) -> AssistantMessage:
        """Stream a response, emitting text/reasoning deltas as they arrive.

        Tool calls arrive as fragments and are reassembled into a complete list
        so the returned :class:`AssistantMessage` matches the non-streaming one.
        """

        kwargs: Dict[str, Any] = {
            "model": self.profile.model,
            "messages": list(messages),
            "temperature": self.profile.temperature,
            "top_p": self.profile.top_p,
            "max_tokens": self.profile.max_tokens,
            "stream": True,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        content_parts: List[str] = []
        reasoning_parts: List[str] = []
        tool_acc: Dict[int, Dict[str, str]] = {}
        finish_reason: Optional[str] = None
        model = self.profile.model
        usage = None

        stream = self._client.chat.completions.create(**kwargs)
        for chunk in stream:
            chunk_model = getattr(chunk, "model", None)
            if chunk_model:
                model = chunk_model
            chunk_usage = getattr(chunk, "usage", None)
            if chunk_usage is not None:
                usage = chunk_usage
            choices = getattr(chunk, "choices", None) or []
            if not choices:
                continue
            choice = choices[0]
            delta = getattr(choice, "delta", None)

            content = getattr(delta, "content", None) if delta else None
            if content:
                content_parts.append(content)
                if on_delta:
                    on_delta(content, "")

            reasoning = _extract_delta_reasoning(delta) if delta else None
            if reasoning:
                reasoning_parts.append(reasoning)
                if on_delta:
                    on_delta("", reasoning)

            for tc in (getattr(delta, "tool_calls", None) or []) if delta else []:
                idx = tc.index if getattr(tc, "index", None) is not None else 0
                slot = tool_acc.setdefault(
                    idx, {"id": "", "name": "", "arguments": ""}
                )
                tc_id = getattr(tc, "id", None)
                if tc_id:
                    slot["id"] += tc_id
                function = getattr(tc, "function", None)
                if function is not None:
                    name = getattr(function, "name", None)
                    if name:
                        slot["name"] += name
                    arguments = getattr(function, "arguments", None)
                    if arguments:
                        slot["arguments"] += arguments

            finish_reason = getattr(choice, "finish_reason", None) or finish_reason

        tool_calls: List[ToolCall] = []
        for index, slot in sorted(tool_acc.items()):
            tool_calls.append(
                ToolCall(
                    id=slot["id"] or f"call_{index}",
                    name=slot["name"],
                    arguments=slot["arguments"],
                )
            )

        raw: Dict[str, Any] = {
            "model": model,
            "finish_reason": finish_reason,
            "stream": True,
        }
        if usage is not None:
            raw["usage"] = {
                "prompt_tokens": getattr(usage, "prompt_tokens", None),
                "completion_tokens": getattr(usage, "completion_tokens", None),
            }

        # Reasoning arrives as continuous fragments (often single characters);
        # concatenate them directly, do NOT insert newlines between fragments.
        reasoning = "".join(reasoning_parts).strip() or None
        return AssistantMessage(
            content="".join(content_parts) or None,
            tool_calls=tool_calls,
            raw=raw,
            reasoning=reasoning,
        )
