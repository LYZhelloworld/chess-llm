"""The LLM turn loop: prompt, tool calls, nudges and the random fallback."""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Protocol

from src.agent.llm_client import REASONING_KEY, AssistantMessage, ToolCall
from src.config import AppConfig
from src.engine.board import ChessEngine
from src.prompts import (
    FALLBACK_NOTICE,
    NUDGE_NO_MOVE,
    system_prompt,
    turn_prompt,
)
from src.tools.executor import ToolExecutor
from src.tools.schema import TOOL_DEFINITIONS

LOGGER = logging.getLogger("src.agent.loop")

EmitFn = Callable[[Dict[str, Any]], None]


class ChatClient(Protocol):
    """Anything that can answer a chat request with tool calling."""

    def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> AssistantMessage: ...


class ChatMemory:
    """Full conversation history of one agent, plus a sliding send window.

    The history is stored in full (it is persisted to the game JSON file), but
    only the most recent ``max_turns`` turns are sent to the model. A "turn" is
    one user message plus everything that follows it until the next user
    message, which guarantees that ``tool_calls`` and their ``tool`` results are
    never separated.
    """

    def __init__(self, messages: Optional[List[Dict[str, Any]]] = None) -> None:
        self.messages: List[Dict[str, Any]] = list(messages or [])

    def add(self, message: Dict[str, Any]) -> None:
        self.messages.append(message)

    def extend(self, messages: List[Dict[str, Any]]) -> None:
        self.messages.extend(messages)

    def turns(self) -> List[List[Dict[str, Any]]]:
        groups: List[List[Dict[str, Any]]] = []
        for message in self.messages:
            if message.get("role") == "user" or not groups:
                groups.append([message])
            else:
                groups[-1].append(message)
        return groups

    def window(
        self, max_turns: int, keep_reasoning: bool = True
    ) -> List[Dict[str, Any]]:
        groups = self.turns()
        if max_turns and max_turns > 0:
            groups = groups[-max_turns:]
        windowed: List[Dict[str, Any]] = []
        for group in groups:
            windowed.extend(group if keep_reasoning else _without_reasoning(group))
        # Drop leading orphan tool results so the API never sees a result
        # without the tool call that produced it.
        while windowed and windowed[0].get("role") == "tool":
            windowed.pop(0)
        return windowed


def _ask_client(client: Any, method: str, default: bool) -> bool:
    """Ask the client something it may not implement (scripted test clients)."""

    function = getattr(client, method, None)
    return default if not callable(function) else bool(function())


def _without_reasoning(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Strip the stored chain of thought before sending history to a backend.

    The reasoning is kept in the transcript (and in the game JSON) because it
    is useful to read, but most backends reject it on the way back in.
    """

    cleaned: List[Dict[str, Any]] = []
    for message in messages:
        if REASONING_KEY in message:
            message = {k: v for k, v in message.items() if k != REASONING_KEY}
        cleaned.append(message)
    return cleaned


@dataclass
class TurnOutcome:
    move_san: Optional[str] = None
    move_uci: Optional[str] = None
    resigned: bool = False
    resign_reason: Optional[str] = None
    fallback_random: bool = False
    spoken: List[str] = field(default_factory=list)
    tool_events: List[Dict[str, Any]] = field(default_factory=list)
    transcript: List[Dict[str, Any]] = field(default_factory=list)
    iterations: int = 0
    error: Optional[str] = None


def run_llm_turn(
    client: ChatClient,
    engine: ChessEngine,
    side: str,
    memory: ChatMemory,
    config: AppConfig,
    emit: Optional[EmitFn] = None,
    rng: Optional[random.Random] = None,
    executor_factory: Optional[Callable[[ChessEngine, str], ToolExecutor]] = None,
    keep_reasoning: Optional[bool] = None,
) -> TurnOutcome:
    """Run one agent turn until it plays a move, resigns or runs out of turns.

    ``keep_reasoning`` says whether the stored chain of thought may be sent back
    to the model; ``None`` falls back to what the client reports.
    """

    emit = emit or (lambda _event: None)
    rng = rng or random.Random()
    outcome = TurnOutcome()
    if keep_reasoning is None:
        keep_reasoning = _ask_client(client, "reasoning_enabled", True)

    turn_message: Dict[str, Any] = {"role": "user", "content": turn_prompt(engine, side)}
    memory.add(turn_message)
    outcome.transcript.append(turn_message)
    emit({"type": "user_prompt", "side": side, "content": turn_message["content"]})

    make_executor = executor_factory or (
        lambda eng, s: ToolExecutor(eng, s, config.draw)
    )
    executor = make_executor(engine, side)

    # Streaming is opt-in per client; scripted test clients simply ignore it.
    # Only pass the extra kwargs when the client advertises streaming support,
    # so backends that do not implement them keep working unchanged.
    stream_requested = _ask_client(client, "streaming_enabled", False)

    def _emit_delta(content_delta: str, reasoning_delta: str) -> None:
        emit(
            {
                "type": "llm_delta",
                "side": side,
                "content": content_delta,
                "reasoning": reasoning_delta,
            }
        )

    chat_kwargs: Dict[str, Any] = {}
    if stream_requested:
        chat_kwargs["stream"] = True
        chat_kwargs["on_delta"] = _emit_delta

    for iteration in range(1, config.loop.max_tool_turns + 1):
        outcome.iterations = iteration
        messages = [{"role": "system", "content": system_prompt(side)}]
        messages.extend(memory.window(config.loop.context_turns, keep_reasoning))

        try:
            response = client.chat(messages, TOOL_DEFINITIONS, **chat_kwargs)
        except Exception as exc:  # noqa: BLE001 - backend failure ends the turn
            # Some backends reject a stored chain of thought instead of
            # ignoring it; in "auto" mode give them one chance without it.
            if (
                keep_reasoning
                and any(REASONING_KEY in m for m in messages)
                and _ask_client(client, "reasoning_refused", False)
            ):
                LOGGER.warning(
                    "Request with chain of thought failed (%s); retrying without it.",
                    exc,
                )
                keep_reasoning = False
                try:
                    retry = messages[:1] + _without_reasoning(messages[1:])
                    response = client.chat(retry, TOOL_DEFINITIONS)
                except Exception as retry_exc:  # noqa: BLE001
                    exc = retry_exc
                else:
                    exc = None
            if exc is not None:
                LOGGER.error("LLM turn aborted after backend failure: %s", exc)
                outcome.error = str(exc)
                emit({"type": "llm_error", "side": side, "message": str(exc)})
                return _fallback(engine, side, memory, outcome, config, emit, rng)

        assistant_message = response.to_message()
        memory.add(assistant_message)
        outcome.transcript.append(assistant_message)

        if response.content:
            outcome.spoken.append(response.content)
        emit(
            {
                "type": "llm_message",
                "side": side,
                "content": response.content or "",
                REASONING_KEY: response.reasoning or "",
            }
        )

        if not response.tool_calls:
            nudge: Dict[str, Any] = {"role": "user", "content": NUDGE_NO_MOVE}
            memory.add(nudge)
            outcome.transcript.append(nudge)
            emit({"type": "nudge", "side": side, "content": nudge["content"]})
            continue

        for call in response.tool_calls:
            _run_tool(executor, call, side, memory, outcome, emit)

        if executor.terminal:
            if executor.committed_move:
                outcome.move_san = executor.committed_move
                outcome.move_uci = executor.committed_uci
            if executor.resigned:
                outcome.resigned = True
                outcome.resign_reason = executor.resign_reason
            return outcome

    LOGGER.warning(
        "%s did not play a move within %d exchanges; playing a random legal move.",
        side,
        config.loop.max_tool_turns,
    )
    return _fallback(engine, side, memory, outcome, config, emit, rng)


def _run_tool(
    executor: ToolExecutor,
    call: ToolCall,
    side: str,
    memory: ChatMemory,
    outcome: TurnOutcome,
    emit: EmitFn,
) -> None:
    emit(
        {
            "type": "tool_call",
            "side": side,
            "name": call.name,
            "arguments": call.arguments,
        }
    )
    result = executor.execute(call.name, call.arguments)
    tool_message: Dict[str, Any] = {
        "role": "tool",
        "tool_call_id": call.id,
        "name": call.name,
        "content": result,
    }
    memory.add(tool_message)
    outcome.transcript.append(tool_message)
    outcome.tool_events.append(
        {"name": call.name, "arguments": call.arguments, "result": result}
    )
    emit(
        {
            "type": "tool_result",
            "side": side,
            "name": call.name,
            "result": result,
        }
    )


def _fallback(
    engine: ChessEngine,
    side: str,
    memory: ChatMemory,
    outcome: TurnOutcome,
    config: AppConfig,
    emit: EmitFn,
    rng: random.Random,
) -> TurnOutcome:
    legal = engine.legal_moves_san()
    if not legal:
        outcome.resigned = True
        outcome.resign_reason = "no legal move available"
        return outcome

    if not config.loop.fallback_random_move:
        outcome.error = outcome.error or "no move produced and fallback disabled"
        return outcome

    chosen = rng.choice(legal)
    record = engine.apply_san(chosen)
    outcome.move_san = record.san
    outcome.move_uci = record.uci
    outcome.fallback_random = True

    notice: Dict[str, Any] = {
        "role": "user",
        "content": FALLBACK_NOTICE.format(move=record.san),
    }
    memory.add(notice)
    outcome.transcript.append(notice)
    emit(
        {
            "type": "fallback_move",
            "side": side,
            "move": record.san,
            "uci": record.uci,
        }
    )
    LOGGER.info("Random fallback move for %s: %s", side, record.san)
    return outcome
