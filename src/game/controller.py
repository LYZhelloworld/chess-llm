"""The game state machine: whose turn it is, what happens after a move."""

from __future__ import annotations

import logging
import random
import re
from typing import Any, Callable, Dict, List, Optional

from src.agent.llm_client import LlmClient
from src.agent.loop import ChatMemory, TurnOutcome, run_llm_turn
from src.config import AppConfig, BLACK, HUMAN, LLM, WHITE
from src.engine.board import ChessEngine, MoveError
from src.engine.rules import GameResult, evaluate_position, resignation_result
from src.game.state import GameState
from src.game.storage import StoragePaths, save_game

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
        self.rng = rng or random.Random(state.seed)

        self.engine = ChessEngine(state.start_fen)
        for san in state.moves_san:
            self.engine.apply_san(san)

        self.memories: Dict[str, ChatMemory] = {
            WHITE: ChatMemory(state.white_chat_history),
            BLACK: ChatMemory(state.black_chat_history),
        }
        self._clients: Dict[str, LlmClient] = {}

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
            "white_name": self.state.white.display_name,
            "black_name": self.state.black.display_name,
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
        if self.state.finished:
            return self._error("illegal_state", "The game is already finished.")
        result = resignation_result(side, reason)
        LOGGER.info("%s resigned: %s", side, reason or "(no reason given)")
        self._finish(result)
        return {"ok": True, "resigned": side, "result": result.to_dict()}

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
            )
        except Exception as exc:  # noqa: BLE001 - never kill the game loop
            LOGGER.exception("LLM turn for %s crashed: %s", side, exc)
            self.emit({"type": "llm_error", "side": side, "message": str(exc)})
            return {"status": "error", "side": side, "message": str(exc)}

        self.state.set_chat(side, memory.messages)
        for text in outcome.spoken:
            self.emit({"type": "talk", "side": side, "ply": ply_before, "text": text})

        if outcome.resigned:
            result = resignation_result(side, outcome.resign_reason)
            self._finish(result)
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
        """Advance the game by one action; used by the headless runner."""

        if self.state.finished:
            return {"status": "finished", "result": self.state.result}
        if self.current_player.kind == LLM:
            return self.run_llm_turn()
        return {"status": "waiting_human", "side": self.side_to_move}

    # ----------------------------------------------------------------- internals
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
        self._finish(result)

    def _finish(self, result: GameResult) -> None:
        self.state.result = result.to_dict()
        self.state.finished = True
        LOGGER.info("Game over: %s (%s)", result.reason, result.detail)
        self.save()
        self.emit({"type": "game_over", "result": result.to_dict()})

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
