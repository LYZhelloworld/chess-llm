import json

from src.config import DrawConfig
from src.engine.board import ChessEngine
from src.tools.executor import ToolExecutor


def call(executor, name, arguments=None):
    raw = json.dumps(arguments or {})
    return json.loads(executor.execute(name, raw))


def test_get_board_returns_fen_and_status():
    ex = ToolExecutor(ChessEngine(), "white")
    payload = call(ex, "get_board")
    assert payload["ok"] is True
    assert payload["side_to_move"] == "white"
    assert payload["check_status"] == "no check"
    assert payload["castling_rights"] == "KQkq"


def test_get_legal_moves_returns_san_only():
    ex = ToolExecutor(ChessEngine(), "white")
    payload = call(ex, "get_legal_moves")
    assert payload["count"] == 20
    assert all(isinstance(m, str) for m in payload["moves"])
    assert "e4" in payload["moves"]


def test_get_board_ascii_contains_a_board():
    ex = ToolExecutor(ChessEngine(), "white")
    payload = call(ex, "get_board_ascii")
    assert payload["ascii"].splitlines()[0].startswith("r n b q k b n r")


def test_make_move_commits_and_ends_the_turn():
    engine = ChessEngine()
    ex = ToolExecutor(engine, "white")
    payload = call(ex, "make_move", {"move": "e4"})
    assert payload["ok"] is True
    assert payload["committed"] is True
    assert ex.terminal is True
    assert engine.ply == 1

    second = ToolExecutor(engine, "white")
    blocked = call(second, "make_move", {"move": "d4"})
    assert blocked["ok"] is False


def test_make_move_rejects_moves_outside_the_legal_list():
    ex = ToolExecutor(ChessEngine(), "white")
    payload = call(ex, "make_move", {"move": "e5"})
    assert payload["ok"] is False
    assert payload["error"]["category"] == "illegal_move"
    assert "get_legal_moves" in payload["error"]["hint"]


def test_make_move_reports_ambiguous_moves_and_lets_you_disambiguate():
    # White has knights on d2 and g1, both able to reach f3 -> "Nf3" is ambiguous.
    engine = ChessEngine("4k3/8/8/8/8/8/3N3K/6N1 w - - 0 1")
    ex = ToolExecutor(engine, "white")

    ambiguous = call(ex, "make_move", {"move": "Nf3"})
    assert ambiguous["ok"] is False
    assert ambiguous["error"]["category"] == "ambiguous_move"
    assert "disambiguate" in ambiguous["error"]["hint"].lower()
    # The raw engine message (e.g. "ambiguous san: 'Nf3' ...") is surfaced.
    assert "ambiguous" in ambiguous["error"]["message"]

    # The disambiguated form is accepted.
    resolved = call(ex, "make_move", {"move": "Ndf3"})
    assert resolved["ok"] is True


def test_make_move_illegal_error_keeps_the_raw_engine_message():
    ex = ToolExecutor(ChessEngine(), "white")
    payload = call(ex, "make_move", {"move": "e5"})
    # classify_exception preserves the original python-chess text in the message.
    assert "Illegal move" in payload["error"]["message"]
    assert "legal_moves_sample" in payload["error"]["detail"]



def test_make_move_rejects_bad_argument_types():
    ex = ToolExecutor(ChessEngine(), "white")
    payload = call(ex, "make_move", {"move": 42})
    assert payload["ok"] is False
    assert payload["error"]["category"] == "invalid_input"


def test_try_make_move_supports_multiple_moves_and_optional_fen():
    engine = ChessEngine()
    ex = ToolExecutor(engine, "white")
    payload = call(ex, "try_make_move", {"moves": ["e4", "e5", "Nf3"]})
    assert payload["ok"] is True
    assert payload["committed"] is False
    assert engine.ply == 0, "try_make_move must never touch the real board"

    from_fen = call(
        ex,
        "try_make_move",
        {"fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1", "moves": ["d4", "d5"]},
    )
    assert from_fen["ok"] is True
    assert from_fen["moves_applied"] == 2


def test_try_make_move_reports_alternation_violations():
    ex = ToolExecutor(ChessEngine(), "white")
    payload = call(ex, "try_make_move", {"moves": ["e4", "Nf3"]})
    assert payload["ok"] is False
    assert payload["error"]["category"] in {"illegal_move", "invalid_input"}
    assert "alternate" in payload["message"]


def test_try_make_move_rejects_bad_fen():
    ex = ToolExecutor(ChessEngine(), "white")
    payload = call(ex, "try_make_move", {"fen": "nonsense", "moves": ["e4"]})
    assert payload["ok"] is False
    assert payload["error"]["category"] == "invalid_input"


def test_resign_marks_the_turn_terminal():
    ex = ToolExecutor(ChessEngine(), "black")
    payload = call(ex, "resign", {"reason": "lost the queen"})
    assert payload["ok"] is True
    assert ex.resigned is True
    assert ex.resign_reason == "lost the queen"


def test_unknown_tool_is_reported_not_raised():
    ex = ToolExecutor(ChessEngine(), "white")
    payload = call(ex, "cast_fireball")
    assert payload["ok"] is False
    assert payload["error"]["category"] == "unknown_tool"


def test_malformed_json_arguments_are_reported():
    ex = ToolExecutor(ChessEngine(), "white")
    payload = json.loads(ex.execute("make_move", "{not json"))
    assert payload["ok"] is False
    assert payload["error"]["category"] == "invalid_input"


def test_terminal_turn_blocks_further_committing_tools():
    engine = ChessEngine()
    ex = ToolExecutor(engine, "white", DrawConfig())
    call(ex, "make_move", {"move": "e4"})
    payload = call(ex, "resign")
    assert payload["ok"] is False
    assert payload["error"]["category"] == "illegal_state"
