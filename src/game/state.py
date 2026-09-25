"""Serializable game state."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import chess

from src.agent.llm_client import REASONING_KEY
from src.config import HUMAN, LLM, WHITE, BLACK


@dataclass
class PlayerSpec:
    """Who controls one side: a human or an LLM profile."""

    kind: str = HUMAN
    profile: str = "default"
    model: str = ""
    label: str = ""

    @property
    def is_llm(self) -> bool:
        return self.kind == LLM

    @property
    def display_name(self) -> str:
        if self.label:
            return self.label
        if self.kind == LLM:
            return f"LLM:{self.model or self.profile}"
        return "Human"

    def to_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "profile": self.profile, "model": self.model, "label": self.label}

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "PlayerSpec":
        data = data or {}
        return cls(
            kind=data.get("kind", HUMAN),
            profile=data.get("profile", "default"),
            model=data.get("model", ""),
            label=data.get("label", ""),
        )

    @classmethod
    def human(cls) -> "PlayerSpec":
        return cls(kind=HUMAN)

    @classmethod
    def llm(cls, profile: str, model: str = "") -> "PlayerSpec":
        return cls(kind=LLM, profile=profile, model=model)


@dataclass
class GameState:
    """Everything that is persisted for one game."""

    game_id: str
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    start_fen: str = chess.STARTING_FEN
    white: PlayerSpec = field(default_factory=PlayerSpec.human)
    black: PlayerSpec = field(default_factory=PlayerSpec.llm)
    moves_san: List[str] = field(default_factory=list)
    moves_uci: List[str] = field(default_factory=list)
    white_chat_history: List[Dict[str, Any]] = field(default_factory=list)
    black_chat_history: List[Dict[str, Any]] = field(default_factory=list)
    result: Optional[Dict[str, Any]] = None
    finished: bool = False
    seed: Optional[int] = None

    # ------------------------------------------------------------------ helpers
    @property
    def mode(self) -> str:
        left = LLM if self.white.is_llm else HUMAN
        right = LLM if self.black.is_llm else HUMAN
        return f"{left}-vs-{right}"

    def player(self, side: str) -> PlayerSpec:
        return self.white if side == WHITE else self.black

    def chat(self, side: str) -> List[Dict[str, Any]]:
        return self.white_chat_history if side == WHITE else self.black_chat_history

    def set_chat(self, side: str, messages: List[Dict[str, Any]]) -> None:
        if side == WHITE:
            self.white_chat_history = messages
        else:
            self.black_chat_history = messages

    def _turn_for_ply(self, ply: int) -> Tuple[str, int]:
        """Map a 0-based ply index to the side that played it and its turn index."""

        side = WHITE if ply % 2 == 0 else BLACK
        return side, ply // 2

    def comment_for_ply(self, ply: int) -> str:
        """Public remarks of the side that played ``ply`` (0-based)."""

        side, index = self._turn_for_ply(ply)
        turns = chat_turns(self.chat(side))
        if index >= len(turns):
            return ""
        parts = [
            (message.get("content") or "").strip()
            for message in turns[index]
            if message.get("role") == "assistant"
        ]
        return " | ".join(part for part in parts if part)

    def conversation(self) -> List[Dict[str, Any]]:
        """What both agents said, interleaved in the order it was said.

        One entry per move: the several assistant messages of a single turn
        (typically one that calls a tool, one that plays the move) are merged
        and separated by a Markdown horizontal rule, so the UI shows one box
        per move instead of one per message. Tool calls, tool results and
        prompts are left out entirely.
        """

        white_turns = chat_turns(self.white_chat_history)
        black_turns = chat_turns(self.black_chat_history)
        entries: List[Dict[str, Any]] = []
        for index in range(max(len(white_turns), len(black_turns))):
            for side, turns in ((WHITE, white_turns), (BLACK, black_turns)):
                if index >= len(turns):
                    continue
                ply = index * 2 + (1 if side == WHITE else 2)
                said = merge_assistant_speech(turns[index])
                if said is None:
                    continue
                entries.append(
                    {
                        "ply": ply,
                        "side": side,
                        "content": said["content"],
                        REASONING_KEY: said[REASONING_KEY],
                    }
                )
        return entries

    # ----------------------------------------------------------- serialization
    def to_dict(self) -> Dict[str, Any]:
        return {
            "game_id": self.game_id,
            "created_at": self.created_at,
            "mode": self.mode,
            "start_fen": self.start_fen,
            "white": self.white.to_dict(),
            "black": self.black.to_dict(),
            "moves_san": list(self.moves_san),
            "moves_uci": list(self.moves_uci),
            "white_chat_history": self.white_chat_history,
            "black_chat_history": self.black_chat_history,
            "result": self.result,
            "finished": self.finished,
            "seed": self.seed,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "GameState":
        from src.engine.rules import GameResult

        state = cls(
            game_id=str(data.get("game_id") or new_game_id()),
            created_at=data.get("created_at") or datetime.now().isoformat(timespec="seconds"),
            start_fen=data.get("start_fen") or chess.STARTING_FEN,
            white=PlayerSpec.from_dict(data.get("white")),
            black=PlayerSpec.from_dict(data.get("black")),
            moves_san=list(data.get("moves_san") or []),
            moves_uci=list(data.get("moves_uci") or []),
            white_chat_history=list(data.get("white_chat_history") or []),
            black_chat_history=list(data.get("black_chat_history") or []),
            result=GameResult.from_dict(data.get("result")).to_dict()
            if data.get("result")
            else None,
            finished=bool(data.get("finished", False)),
            seed=data.get("seed"),
        )
        return state


def merge_assistant_speech(
    turn: List[Dict[str, Any]], separator: str = "\n\n---\n\n"
) -> Optional[Dict[str, Any]]:
    """Fold every assistant message of one turn into a single spoken text.

    Returns ``None`` when the model said nothing at all, so the caller can skip
    the entry. Repeated identical remarks are dropped - some models echo the
    same sentence on the tool-calling message and on the move message.
    """

    contents: List[str] = []
    reasonings: List[str] = []
    for message in turn:
        if message.get("role") != "assistant":
            continue
        content = (message.get("content") or "").strip()
        reasoning = (message.get(REASONING_KEY) or "").strip()
        if content and content not in contents:
            contents.append(content)
        if reasoning and reasoning not in reasonings:
            reasonings.append(reasoning)
    if not contents and not reasonings:
        return None
    return {
        "content": separator.join(contents),
        REASONING_KEY: separator.join(reasonings),
    }


def chat_turns(messages: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    """Split a flat history into turns; a turn starts at every user message.

    Mirrors ``ChatMemory.turns()``: it is what lets a ply be mapped back to the
    exchange that produced it, so no separate remark list has to be stored.
    """

    turns: List[List[Dict[str, Any]]] = []
    for message in messages:
        if message.get("role") == "user" or not turns:
            turns.append([message])
        else:
            turns[-1].append(message)
    return turns


def new_game_id() -> str:
    from src.game.storage import new_game_id as _new_game_id

    return _new_game_id()
