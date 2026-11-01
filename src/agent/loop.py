"""The LLM turn loop: prompt, tool calls, nudges and the random fallback."""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Protocol

from src.agent.llm_client import REASONING_KEY, AssistantMessage, ToolCall
from src.config import AppConfig
from src.engine.board import ChessEngine
from src.engine.rules import GameResult, evaluate_position, resignation_result
from src.prompts import (
    FALLBACK_NOTICE,
    NUDGE_NO_MOVE,
    farewell_prompt,
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
            windowed.extend(_for_backend(group, keep_reasoning))
        # Drop leading orphan tool results so the API never sees a result
        # without the tool call that produced it.
        while windowed and windowed[0].get("role") == "tool":
            windowed.pop(0)
        return windowed


def _ask_client(client: Any, method: str, default: bool) -> bool:
    """Ask the client something it may not implement (scripted test clients)."""

    function = getattr(client, method, None)
    return default if not callable(function) else bool(function())


# Keys a backend understands on a chat message. Everything else the transcript
# carries (the chain of thought, the shared remark counter, the ply a remark
# belongs to, the human-speech flag) is bookkeeping for us and is stripped here.
_API_MESSAGE_KEYS = frozenset(
    {"role", "content", "name", "tool_calls", "tool_call_id"}
)


def _for_backend(
    messages: List[Dict[str, Any]], keep_reasoning: bool = True
) -> List[Dict[str, Any]]:
    """Reduce transcript messages to what a backend accepts.

    The chain of thought is kept in the transcript (and in the game JSON)
    because it is useful to read, but most backends reject it on the way back
    in - hence the switch. The reply stamps never leave this process.
    """

    allowed = _API_MESSAGE_KEYS | {REASONING_KEY} if keep_reasoning else _API_MESSAGE_KEYS
    return [
        {key: value for key, value in message.items() if key in allowed}
        for message in messages
    ]


@dataclass
class TurnOutcome:
    move_san: Optional[str] = None
    move_uci: Optional[str] = None
    resigned: bool = False
    resign_reason: Optional[str] = None
    fallback_random: bool = False
    # Texts accepted by the ``speak`` tool, in the order they were said. Plain
    # model prose is private and never lands here.
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
    opponent_speech: Optional[str] = None,
    stamp: Optional[Callable[[Dict[str, Any], int], None]] = None,
) -> TurnOutcome:
    """Run one agent turn until it plays a move, resigns or runs out of turns.

    ``keep_reasoning`` says whether the stored chain of thought may be sent back
    to the model; ``None`` falls back to what the client reports. ``stamp``
    marks a message that carries speech with its place on the game's shared
    timeline (see ``src.game.state``); without it the remarks still work, they
    just cannot be ordered against the opponent's once the page is reloaded.
    """

    emit = emit or (lambda _event: None)
    rng = rng or random.Random()
    outcome = TurnOutcome()
    if keep_reasoning is None:
        keep_reasoning = _ask_client(client, "reasoning_enabled", True)

    turn_message: Dict[str, Any] = {
        "role": "user",
        "content": turn_prompt(engine, side, opponent_speech=opponent_speech),
    }
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
                    retry = messages[:1] + _for_backend(messages[1:], keep_reasoning=False)
                    response = client.chat(retry, TOOL_DEFINITIONS)
                except Exception as retry_exc:  # noqa: BLE001
                    exc = retry_exc
                else:
                    exc = None
            if exc is not None:
                LOGGER.error("LLM turn aborted after backend failure: %s", exc)
                outcome.error = str(exc)
                emit({"type": "llm_error", "side": side, "message": str(exc)})
                return _fallback(engine, side, memory, outcome, emit, rng)

        assistant_message = response.to_message()
        # A message that speaks gets its timeline slot now: a human may type
        # while the model is still thinking, and their remark has to sort before
        # everything this answer goes on to say.
        if stamp is not None and any(call.name == "speak" for call in response.tool_calls):
            stamp(assistant_message, engine.ply + 1)
        memory.add(assistant_message)
        outcome.transcript.append(assistant_message)

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
            last_words(
                client=client,
                engine=engine,
                side=side,
                memory=memory,
                config=config,
                result=_final_result(engine, side, executor, config),
                emit=emit,
                outcome=outcome,
                keep_reasoning=keep_reasoning,
                chat_kwargs=chat_kwargs,
                stamp=stamp,
            )
            return outcome

    LOGGER.warning(
        "%s did not play a move within %d exchanges; playing a random legal move.",
        side,
        config.loop.max_tool_turns,
    )
    outcome = _fallback(engine, side, memory, outcome, emit, rng)
    # A random move can finish the game too (mate, stalemate), and the model
    # still deserves its closing line.
    if evaluate_position(engine.board, config.draw) is not None:
        last_words(
            client=client,
            engine=engine,
            side=side,
            memory=memory,
            config=config,
            emit=emit,
            outcome=outcome,
            keep_reasoning=keep_reasoning or False,
            chat_kwargs=chat_kwargs,
            stamp=stamp,
        )
    return outcome


def _final_result(
    engine: ChessEngine, side: str, executor: ToolExecutor, config: AppConfig
) -> Optional[GameResult]:
    """How the turn ended, for the closing prompt.

    A resignation leaves a perfectly playable board behind, so it has to be read
    off the tool that caused it rather than off the position.
    """

    if executor.resigned:
        return resignation_result(side, executor.resign_reason)
    return evaluate_position(engine.board, config.draw)


def last_words(
    client: ChatClient,
    engine: ChessEngine,
    side: str,
    memory: ChatMemory,
    config: AppConfig,
    result: Optional[GameResult] = None,
    emit: Optional[EmitFn] = None,
    outcome: Optional[TurnOutcome] = None,
    keep_reasoning: bool = True,
    chat_kwargs: Optional[Dict[str, Any]] = None,
    stamp: Optional[Callable[[Dict[str, Any], int], None]] = None,
    opponent_speech: Optional[str] = None,
) -> None:
    """One closing exchange once the game has ended.

    The turn returns as soon as the committing tool runs, so a model that has
    just delivered mate - or resigned - would otherwise never get to say
    anything about it. This gives it a single request to do so. Exactly one:
    the result is decided, so retrying or arguing about it would only add noise
    and latency, and a failure here is logged and swallowed because the game is
    over either way.

    ``result`` is how the game ended; when it is omitted the board is asked,
    which is right for mate and the draws but not for a resignation. The
    controller calls this for the seat that did *not* end the game, so both
    players of a finished game get their last word.
    """

    if result is None:
        result = evaluate_position(engine.board, config.draw)
    if result is None:
        return

    emit = emit or (lambda _event: None)
    outcome = outcome or TurnOutcome()
    chat_kwargs = chat_kwargs or {}

    # The turn's own executor is already gone by the time the controller asks
    # for a remark, so this exchange gets a terminal one of its own: `speak` is
    # the only tool left open on it.
    executor = ToolExecutor(engine, side, config.draw)
    executor.terminal = True
    executor.committed_move = outcome.move_san

    message: Dict[str, Any] = {
        "role": "user",
        "content": farewell_prompt(side, result, opponent_speech),
    }
    memory.add(message)
    outcome.transcript.append(message)
    emit({"type": "user_prompt", "side": side, "content": message["content"]})

    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": system_prompt(side)}
    ]
    messages.extend(memory.window(config.loop.context_turns, keep_reasoning))
    try:
        response = client.chat(messages, TOOL_DEFINITIONS, **chat_kwargs)
    except Exception as exc:  # noqa: BLE001 - the game is already over
        LOGGER.warning("Closing remark for %s could not be requested: %s", side, exc)
        return

    assistant_message = response.to_message()
    if stamp is not None and any(call.name == "speak" for call in response.tool_calls):
        # 0 = said after the game: it belongs to no move, so it never turns into
        # a PGN comment.
        stamp(assistant_message, 0)
    memory.add(assistant_message)
    outcome.transcript.append(assistant_message)
    emit(
        {
            "type": "llm_message",
            "side": side,
            "content": response.content or "",
            REASONING_KEY: response.reasoning or "",
        }
    )
    # The executor is already terminal, so anything except `speak` (and the
    # read-only tools) comes back as illegal_state - exactly what the prompt
    # asked the model not to do.
    for call in response.tool_calls:
        _run_tool(executor, call, side, memory, outcome, emit)


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
    said_before = len(executor.said)
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
    # The executor keeps only the remarks it accepted, so a rejected call never
    # reaches the conversation - the same thing the rebuilt view will show.
    if call.name == "speak" and len(executor.said) > said_before:
        outcome.spoken.append(executor.said[-1])
        emit({"type": "say", "side": side, "text": executor.said[-1]})


def _fallback(
    engine: ChessEngine,
    side: str,
    memory: ChatMemory,
    outcome: TurnOutcome,
    emit: EmitFn,
    rng: random.Random,
) -> TurnOutcome:
    """Play a random legal move so a turn always changes the board.

    There is deliberately no switch to turn this off. A turn that ends without
    a move leaves the same side to move, and the driver starts the next turn
    immediately - which turns a model that only talks (or a backend that keeps
    failing) into an endless retry loop that spends the user's credits. An
    agent that wants out of a position can still resign through its own
    ``resign`` tool; the fallback only guarantees the board moves on.
    """

    legal = engine.legal_moves_san()
    if not legal:
        outcome.resigned = True
        outcome.resign_reason = "no legal move available"
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
