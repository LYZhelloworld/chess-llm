"""The game state machine: whose turn it is, what happens after a move."""

from __future__ import annotations

import logging
import random
import re
from typing import Any, Callable, Dict, List, Optional

from src.agent.llm_client import LlmClient
from src.agent.loop import ChatMemory, TurnOutcome, last_words, run_llm_turn
from src.config import AppConfig, BLACK, HUMAN, LLM, WHITE
from src.engine.board import ChessEngine, MoveError
from src.engine.rules import GameResult, evaluate_position, resignation_result
from src.game.state import SEQ_KEY, GameState, speech_entries
from src.game.storage import StoragePaths, save_game
from src.tools.schema import SPEAK_MAX_CHARS

LOGGER = logging.getLogger("src.game.controller")

UCI_RE = re.compile(r"^[a-h][1-8][a-h][1-8][qrbn]?$")


class GameController:
    """Drives one game: human input, LLM turns, terminal detection, saving."""

    def __init__(
        self,
        state: GameState,
        config: AppConfig,
        paths: StoragePaths,
        emit: Optional[Callable[[Dict[str, Any]], None]] = None,
        rng: Optional[random.Random] = None,
    ) -> None:
        self.state = state
        self.config = config
        self.paths = paths
        self._emit = emit or (lambda _event: None)
        self.rng = rng or random.Random()

        self.engine = ChessEngine(state.start_fen)
        for san in state.moves_san:
            self.engine.apply_san(san)

        self.memories: Dict[str, ChatMemory] = {
            WHITE: ChatMemory(state.white_chat_history),
            BLACK: ChatMemory(state.black_chat_history),
        }
        self._clients: Dict[str, LlmClient] = {}
        # Highest remark number each side has already been told about, so a
        # remark is delivered exactly once and a reloaded game does not replay
        # the whole conversation into the next prompt.
        self._heard: Dict[str, int] = {
            WHITE: self._latest_remark(BLACK),
            BLACK: self._latest_remark(WHITE),
        }
        # Seats that have already had the one closing exchange a finished game
        # allows. Two code paths can ask for it (the turn that ended the game,
        # and the controller for the other seat), so it is kept here.
        self._farewells: set[str] = set()

    # ------------------------------------------------------------------ access
    @property
    def side_to_move(self) -> str:
        return self.engine.side_to_move

    @property
    def current_player(self):
        return self.state.player(self.side_to_move)

    @property
    def finished(self) -> bool:
        return self.state.finished

    def legal_moves(self) -> List[Dict[str, str]]:
        return [{"san": san, "uci": uci} for san, uci in self.engine.legal_moves()]

    def client(self, side: str) -> LlmClient:
        if side not in self._clients:
            spec = self.state.player(side)
            profile = self.config.profile(spec.profile)
            if spec.model:
                profile = profile.override(model=spec.model)
            self._clients[side] = LlmClient(profile)
            if not spec.model:
                spec.model = profile.model
                spec.label = spec.label or f"LLM:{profile.model}"
        return self._clients[side]

    # -------------------------------------------------------------------- view
    def view(self) -> Dict[str, Any]:
        board = self.engine.board
        return {
            "game_id": self.state.game_id,
            "mode": self.state.mode,
            "created_at": self.state.created_at,
            "white": self.state.white.to_dict(),
            "black": self.state.black.to_dict(),
            "white_name": self.state.player_name(WHITE),
            "black_name": self.state.player_name(BLACK),
            "fen": self.engine.fen,
            "ascii": self.engine.ascii,
            "unicode": self.engine.unicode,
            "turn": self.side_to_move,
            "check_status": self.engine.check_status(),
            "castling_rights": self.engine.castling_rights,
            "en_passant": self.engine.en_passant,
            "halfmove_clock": self.engine.halfmove_clock,
            "fullmove_number": self.engine.fullmove_number,
            "legal_moves": self.legal_moves(),
            "moves": self._move_list(),
            "result": self.state.result,
            "finished": self.state.finished,
            "chats": {
                WHITE: self.state.white_chat_history,
                BLACK: self.state.black_chat_history,
            },
            "conversation": self.state.conversation(),
            # The seat allowed to type into the conversation, or null when the
            # input must be disabled (no human seat, or the game is over).
            "say_side": self.say_side,
            "in_check": board.is_check(),
        }

    def _move_list(self) -> List[Dict[str, Any]]:
        entries: List[Dict[str, Any]] = []
        for index, san in enumerate(self.state.moves_san):
            entries.append(
                {
                    "ply": index + 1,
                    "move_number": index // 2 + 1,
                    "side": WHITE if index % 2 == 0 else BLACK,
                    "san": san,
                    "uci": self.state.moves_uci[index]
                    if index < len(self.state.moves_uci)
                    else None,
                }
            )
        return entries

    # ------------------------------------------------------------------ action
    def submit_human_move(self, san_or_uci: str) -> Dict[str, Any]:
        side = self.side_to_move
        if self.state.finished:
            return self._error("illegal_state", "The game is already finished.")
        if self.current_player.kind != HUMAN:
            return self._error(
                "illegal_state", f"It is not the human player's turn (side to move: {side})."
            )

        try:
            record = self._parse_move(san_or_uci)
        except MoveError as err:
            return self._error(err.category, err.message, hint=err.hint)

        self._commit_move(record.san, record.uci, side, actor=HUMAN)
        return {"ok": True, "move": record.san, "uci": record.uci, "side": side}

    def resign(self, side: str, reason: Optional[str] = None) -> Dict[str, Any]:
        """End the game by resignation.

        Only a seat a human actually holds can resign: an API client must not be
        able to make the model resign, and in a hot-seat game the seat that is
        allowed to act is the side to move (the same rule the UI applies).
        """

        if self.state.finished:
            return self._error("illegal_state", "The game is already finished.")
        side = (side or "").strip().lower()
        if side not in (WHITE, BLACK):
            return self._error(
                "invalid_input", "Invalid input: side must be 'white' or 'black'."
            )
        if reason is not None and not isinstance(reason, str):
            return self._error(
                "invalid_input", "Invalid input: reason must be a string when provided."
            )
        if self.state.player(side).kind != HUMAN:
            return self._error(
                "illegal_state",
                f"{side.capitalize()} is an LLM seat; only a human can resign.",
            )
        if self._hot_seat() and side != self.side_to_move:
            return self._error(
                "illegal_state",
                f"It is {self.side_to_move}'s turn; only the side to move can "
                "resign in a hot-seat game.",
            )

        reason = (reason or "").strip() or None
        result = resignation_result(side, reason)
        LOGGER.info("%s resigned: %s", side, reason or "(no reason given)")
        self._finish(result, mover=side)
        return {"ok": True, "resigned": side, "result": result.to_dict()}

    def _hot_seat(self) -> bool:
        """Both seats are human, so the side to move is the one acting."""

        return self.state.white.kind == HUMAN and self.state.black.kind == HUMAN

    @property
    def say_side(self) -> Optional[str]:
        """Which seat may speak right now, or ``None`` if nobody may.

        A single human plays their own seat; in a hot-seat game the same screen
        speaks for whoever is to move, so the remark is attributed to the side
        that is actually playing. A finished game keeps the box open - the two
        players can still shake hands - and only a matchup without a human seat
        is silent.
        """

        if self._hot_seat():
            return self.side_to_move
        humans = [side for side in (WHITE, BLACK) if self.state.player(side).kind == HUMAN]
        return humans[0] if humans else None

    def say(self, side: str, text: str) -> Dict[str, Any]:
        """Record a human's remark in the conversation.

        It is stored straight away and handed to the opponent at the start of
        their next turn, so somebody thinking about their move can be heckled
        without interrupting the turn in flight. Once the game is over nobody
        is handed anything any more, but the box stays open for a last word:
        those remarks carry no ply and never become a PGN comment.
        """

        side = (side or "").strip().lower()
        if side not in (WHITE, BLACK):
            return self._error(
                "invalid_input", "Invalid input: side must be 'white' or 'black'."
            )
        if self.state.player(side).kind != HUMAN:
            return self._error(
                "illegal_state",
                f"{side.capitalize()} is an LLM seat; only a human can speak.",
            )
        expected = self.say_side
        # Once the game is over there is no turn left to own the floor, so a
        # hot-seat pair may both say their goodbyes; before that the seat on
        # move is the only one that speaks.
        if expected is not None and side != expected and not self.state.finished:
            return self._error(
                "illegal_state", f"It is {expected}'s turn to speak, not {side}'s."
            )
        if not isinstance(text, str) or not text.strip():
            return self._error(
                "invalid_input", "Invalid input: the message must not be empty."
            )
        text = text.strip()
        if len(text) > SPEAK_MAX_CHARS:
            return self._error(
                "invalid_input",
                f"Invalid input: the message is {len(text)} characters, "
                f"the limit is {SPEAK_MAX_CHARS}.",
            )

        message = self.state.speak(
            side, text, 0 if self.state.finished else self.engine.ply + 1
        )
        LOGGER.info("%s said: %s", side, text)
        self.emit(
            {
                "type": "say",
                "side": side,
                "text": text,
                "seq": message[SEQ_KEY],
            }
        )
        self.save()
        return {"ok": True, "side": side, "text": text, "seq": message[SEQ_KEY]}

    def run_llm_turn(self) -> Dict[str, Any]:
        """Run the LLM turn for whichever side is to move. Blocking."""

        side = self.side_to_move
        if self.state.finished:
            return {"status": "finished", "result": self.state.result}
        if self.current_player.kind != LLM:
            return {"status": "waiting_human", "side": side}

        memory = self.memories[side]
        ply_before = self.engine.ply
        self.emit({"type": "turn_start", "side": side, "ply": ply_before + 1})

        try:
            client = self.client(side)
            outcome: TurnOutcome = run_llm_turn(
                client=client,
                engine=self.engine,
                side=side,
                memory=memory,
                config=self.config,
                emit=self.emit,
                rng=self.rng,
                keep_reasoning=client.reasoning_enabled(),
                opponent_speech=self._opponent_speech(side),
                stamp=self.state.stamp,
            )
        except Exception as exc:  # noqa: BLE001 - never kill the game loop
            LOGGER.exception("LLM turn for %s crashed: %s", side, exc)
            self.emit({"type": "llm_error", "side": side, "message": str(exc)})
            return {"status": "error", "side": side, "message": str(exc)}

        self.state.set_chat(side, memory.messages)

        if outcome.resigned:
            result = resignation_result(side, outcome.resign_reason)
            self._finish(result, mover=side)
            return {
                "status": "resigned",
                "side": side,
                "reason": outcome.resign_reason,
                "result": result.to_dict(),
            }

        if not outcome.move_san:
            LOGGER.error("%s produced no move and no resignation; ending the turn.", side)
            return {"status": "no_move", "side": side}

        self._commit_move(
            outcome.move_san,
            outcome.move_uci or "",
            side,
            actor=LLM,
            fallback=outcome.fallback_random,
        )
        return {
            "status": "moved",
            "side": side,
            "move": outcome.move_san,
            "fallback_random": outcome.fallback_random,
        }

    def step(self) -> Dict[str, Any]:
        """Advance the game by one action (an LLM turn or a human prompt)."""

        if self.state.finished:
            return {"status": "finished", "result": self.state.result}
        if self.current_player.kind == LLM:
            return self.run_llm_turn()
        return {"status": "waiting_human", "side": self.side_to_move}

    # ----------------------------------------------------------------- internals
    def _latest_remark(self, side: str) -> int:
        """Highest remark number in one side's history (0 when it never spoke)."""

        return max(
            (int(m.get(SEQ_KEY) or 0) for m in self.state.chat(side)), default=0
        )

    def _opponent_speech(self, side: str) -> Optional[str]:
        """Everything the opponent said since this side last heard from them.

        Both kinds of remark count - a model's ``speak`` calls and the
        operator's typed lines - and they are delivered once: the watermark is
        advanced here, so the same heckle is not repeated into every prompt for
        the rest of the game.
        """

        opponent = BLACK if side == WHITE else WHITE
        heard = self._heard.get(side, 0)
        fresh = [
            entry["text"]
            for entry in speech_entries(self.state.chat(opponent), opponent)
            if entry[SEQ_KEY] > heard
        ]
        self._heard[side] = max(self._latest_remark(opponent), heard)
        return "\n\n".join(fresh) if fresh else None

    def _parse_move(self, san_or_uci: str):
        text = (san_or_uci or "").strip()
        if not text:
            raise MoveError("invalid_input", "Invalid input: empty move.")
        if UCI_RE.match(text):
            try:
                return self.engine.apply_uci(text)
            except MoveError as err:
                # It may still be a valid SAN such as "b1c3" style notations.
                try:
                    return self.engine.apply_san(text)
                except MoveError:
                    raise err
        return self.engine.apply_san(text)

    def _commit_move(
        self,
        san: str,
        uci: str,
        side: str,
        actor: str,
        fallback: bool = False,
    ) -> None:
        self.state.moves_san.append(san)
        self.state.moves_uci.append(uci)
        LOGGER.info(
            "Move %d: %s (%s) played by %s%s",
            len(self.state.moves_san),
            san,
            uci,
            side,
            " [random fallback]" if fallback else "",
        )
        self.emit(
            {
                "type": "move",
                "side": side,
                "actor": actor,
                "move": san,
                "uci": uci,
                "fen": self.engine.fen,
                "ply": len(self.state.moves_san),
                "fallback_random": fallback,
            }
        )

        result = evaluate_position(self.engine.board, self.config.draw)
        if result is None:
            self.save()
            self.emit({"type": "state", "reason": "move"})
            return
        self._finish(result, mover=side)

    def _finish(self, result: GameResult, mover: Optional[str] = None) -> None:
        """Close the game and let the other seat say goodbye.

        ``mover`` is the side whose action ended the game. It has already had
        its closing exchange inside the turn that ended it, so only the other
        seat is asked here - and only if a model is sitting in it. A human can
        type whenever they like (the box stays open), so this is a no-op there.
        """

        self.state.result = result.to_dict()
        self.state.finished = True
        LOGGER.info("Game over: %s (%s)", result.reason, result.detail)
        self.save()
        self.emit({"type": "game_over", "result": result.to_dict()})
        for side in (WHITE, BLACK):
            if side != mover:
                self._farewell(side, result)

    def _farewell(self, side: str, result: GameResult) -> None:
        """Ask a model seat for its one closing remark.

        The losing seat of a resignation used to get nothing at all, because
        its own turn was the one that ended the game - and the winner of a
        resignation got nothing either, because the opposing turn simply never
        started. Both are asked from here now.
        """

        if side in self._farewells:
            return
        if self.state.player(side).kind != LLM:
            return
        self._farewells.add(side)
        memory = self.memories[side]
        try:
            client = self.client(side)
            last_words(
                client=client,
                engine=self.engine,
                side=side,
                memory=memory,
                config=self.config,
                result=result,
                emit=self.emit,
                keep_reasoning=client.reasoning_enabled(),
                stamp=self.state.stamp,
                # The opponent's goodbye is the one remark this seat has never
                # been given, and there is no next turn to put it in.
                opponent_speech=self._opponent_speech(side),
            )
        except Exception:  # noqa: BLE001 - the game is over either way
            LOGGER.exception("Closing remark for %s failed", side)
            return
        self.state.set_chat(side, memory.messages)
        self.save()

    def save(self) -> None:
        if not self.config.storage.autosave:
            return
        try:
            save_game(self.state, self.paths)
        except Exception:  # noqa: BLE001 - persistence must never break a game
            LOGGER.exception("Failed to save game %s", self.state.game_id)

    def emit(self, event: Dict[str, Any]) -> None:
        try:
            self._emit(event)
        except Exception:  # noqa: BLE001 - UI transport problems are non fatal
            LOGGER.debug("Event delivery failed: %s", event.get("type"))

    def set_emit(self, emit: Optional[Callable[[Dict[str, Any]], None]]) -> None:
        """Bind the event sink, replacing any previous one.

        The CLI builds a controller before the web layer exists, so the server
        attaches its own sink afterwards; without this the first game would run
        silently and the browser would never refresh.
        """

        self._emit = emit or (lambda _event: None)

    def close(self) -> None:
        """Persist the game before the process exits."""

        LOGGER.info("Closing game %s (%d plies)", self.state.game_id, len(self.state.moves_san))
        self.save()
        self.emit({"type": "closed", "game_id": self.state.game_id})

    @staticmethod
    def _error(category: str, message: str, hint: str = "") -> Dict[str, Any]:
        payload: Dict[str, Any] = {"ok": False, "error": {"category": category, "message": message}}
        if hint:
            payload["error"]["hint"] = hint
        return payload
