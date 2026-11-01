"""Serializable game state."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

import chess

from src.config import HUMAN, LLM, WHITE, BLACK
from src.tools.schema import SPEAK_MAX_CHARS

# Bookkeeping carried by a transcript message and stripped again before the
# history is sent to a backend (see ChatMemory.window):
#
# - ``seq`` orders everything said by both sides on one timeline. The two chat
#   histories are separate lists, so without a shared counter a remark made
#   while the opponent was thinking could not be placed after the one it
#   followed.
# - ``ply`` is the 1-based ply the remark belongs to, i.e. the move the speaker
#   was about to play. It is what the PGN comment is built from. ``0`` means the
#   remark was made after the game ended, so it belongs to no move and never
#   becomes a comment.
# - ``spoken`` marks a ``user`` message as a human's public remark. The ``user``
#   messages of an LLM seat are turn prompts, not speech.
SEQ_KEY = "seq"
PLY_KEY = "ply"
SPOKEN_KEY = "spoken"

_PARAGRAPH_RE = re.compile(r"\n\s*\n")


@dataclass
class PlayerSpec:
    """Who controls one side: a human or an LLM profile."""

    kind: str = HUMAN
    # Empty for a human seat: a person is not played by a profile.
    profile: str = ""
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
            profile=data.get("profile", ""),
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
    # Counter behind SEQ_KEY; derived from the histories on first use, so it is
    # not persisted and a loaded game simply continues where it left off.
    _seq: Optional[int] = field(default=None, init=False, repr=False)

    # ------------------------------------------------------------------ helpers
    @property
    def mode(self) -> str:
        left = LLM if self.white.is_llm else HUMAN
        right = LLM if self.black.is_llm else HUMAN
        return f"{left}-vs-{right}"

    def player(self, side: str) -> PlayerSpec:
        return self.white if side == WHITE else self.black

    def player_name(self, side: str) -> str:
        """How one seat is labelled in the UI.

        ``PlayerSpec.display_name`` is fine unless both seats are human: there
        the generic "Human" makes white and black indistinguishable in the
        conversation, so each seat is named after its colour instead.
        """

        spec = self.player(side)
        if spec.label:
            return spec.label
        if spec.kind != LLM and not self.white.is_llm and not self.black.is_llm:
            return side.capitalize()
        return spec.display_name

    def chat(self, side: str) -> List[Dict[str, Any]]:
        return self.white_chat_history if side == WHITE else self.black_chat_history

    def set_chat(self, side: str, messages: List[Dict[str, Any]]) -> None:
        if side == WHITE:
            self.white_chat_history = messages
        else:
            self.black_chat_history = messages

    # ------------------------------------------------------------------ remarks
    def next_seq(self) -> int:
        """Next slot on the shared "who said what, when" timeline."""

        if self._seq is None:
            self._seq = (
                max(
                    (
                        int(message.get(SEQ_KEY) or 0)
                        for side in (WHITE, BLACK)
                        for message in self.chat(side)
                    ),
                    default=0,
                )
                + 1
            )
        value = self._seq
        self._seq += 1
        return value

    def stamp(self, message: Dict[str, Any], ply: int) -> Dict[str, Any]:
        """Pin a transcript message to the shared timeline and to a ply."""

        message[SEQ_KEY] = self.next_seq()
        message[PLY_KEY] = ply
        return message

    def speak(self, side: str, text: str, ply: int) -> Dict[str, Any]:
        """Record a human's public remark in that side's own history.

        A human seat has no model behind it, so the ``user`` role is free to be
        the speaker; ``SPOKEN_KEY`` is what tells it apart from a turn prompt.
        """

        message: Dict[str, Any] = {"role": "user", "content": text, SPOKEN_KEY: True}
        self.stamp(message, ply)
        self.chat(side).append(message)
        return message

    def remarks(self, side: str) -> List[Dict[str, Any]]:
        """Every public remark of one side, in the order it was made."""

        return speech_entries(self.chat(side), side)

    def conversation(self) -> List[Dict[str, Any]]:
        """Both sides' public remarks as one timeline, one paragraph per entry.

        A remark whose text has blank lines in it becomes several entries: the
        UI shows one bubble per paragraph, and the operator reads a paragraph
        as one message. Ordering comes from ``seq`` - see the note at the top of
        this module - so a human interjecting in the middle of a model's turn
        keeps its place.
        """

        entries = self.remarks(WHITE) + self.remarks(BLACK)
        entries.sort(key=lambda entry: entry[SEQ_KEY])
        return entries

    def comment_for_ply(self, ply: int) -> str:
        """Public remarks of the side that played ``ply`` (0-based).

        Only what was said during that side's own turn counts: a remark the
        human makes while the opponent is thinking belongs to the opponent's
        ply, and the opponent is not the one who said it.
        """

        side = WHITE if ply % 2 == 0 else BLACK
        spoken = [
            entry["text"]
            for entry in self.remarks(side)
            if entry.get(PLY_KEY) == ply + 1
        ]
        return " | ".join(spoken)

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
        )
        return state


def speech_entries(
    messages: List[Dict[str, Any]], side: str
) -> List[Dict[str, Any]]:
    """Pull the public remarks out of one chat history, in order.

    Two sources, one shape: a human's remark is a ``user`` message flagged with
    ``SPOKEN_KEY``, a model's is the text argument of a ``speak`` tool call on
    an assistant message. Everything else in the history - turn prompts,
    nudges, tool results, the model's private prose - is not speech.
    """

    entries: List[Dict[str, Any]] = []
    for message in messages:
        seq = int(message.get(SEQ_KEY) or 0)
        ply = message.get(PLY_KEY)
        if message.get(SPOKEN_KEY):
            texts: List[str] = [message.get("content") or ""]
            kind = "say"
        elif message.get("role") == "assistant":
            texts = [
                text
                for text in (
                    _spoken_text(call) for call in message.get("tool_calls") or []
                )
                if text
            ]
            kind = "speak"
        else:
            continue
        for text in texts:
            for paragraph in paragraphs_of(text):
                entries.append(
                    {
                        "side": side,
                        "kind": kind,
                        "text": paragraph,
                        SEQ_KEY: seq,
                        PLY_KEY: ply,
                    }
                )
    return entries


def _spoken_text(call: Dict[str, Any]) -> Optional[str]:
    """The text of one ``speak`` call, or ``None`` when it is not one."""

    function = call.get("function") or {}
    if function.get("name") != "speak":
        return None
    try:
        arguments = json.loads(function.get("arguments") or "{}")
    except (TypeError, ValueError):
        return None
    text = arguments.get("text") if isinstance(arguments, dict) else None
    if not isinstance(text, str):
        return None
    text = text.strip()
    # A rejected call is still in the transcript; it must not show up as speech
    # either, or the rebuilt conversation would display words the tool refused.
    if not text or len(text) > SPEAK_MAX_CHARS:
        return None
    return text


def paragraphs_of(text: str) -> List[str]:
    """Split one remark into the paragraphs the UI shows as separate bubbles."""

    return [block.strip() for block in _PARAGRAPH_RE.split(text or "") if block.strip()]


def new_game_id() -> str:
    from src.game.storage import new_game_id as _new_game_id

    return _new_game_id()
