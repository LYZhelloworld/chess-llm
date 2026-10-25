import chess

from src.config import DrawConfig
from src.engine.board import ChessEngine
from src.engine.errors import (
    CATEGORY_ILLEGAL_MOVE,
    CATEGORY_INVALID_INPUT,
    MoveError,
)
from src.engine.rules import evaluate_position, resignation_result


def test_initial_position_has_twenty_legal_moves():
    engine = ChessEngine()
    assert len(engine.legal_moves()) == 20
    assert "e4" in engine.legal_moves_san()
    assert engine.side_to_move == "white"


def test_apply_san_updates_the_board():
    engine = ChessEngine()
    record = engine.apply_san("e4")
    assert record.san == "e4"
    assert record.uci == "e2e4"
    assert engine.side_to_move == "black"
    assert engine.fen.startswith("rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR")


def test_illegal_move_is_classified():
    engine = ChessEngine()
    try:
        engine.apply_san("e5")
    except MoveError as err:
        assert err.category == CATEGORY_ILLEGAL_MOVE
        assert err.hint
    else:  # pragma: no cover
        raise AssertionError("expected a MoveError")


def test_malformed_notation_is_invalid_input():
    engine = ChessEngine()
    try:
        engine.apply_san("Z9")
    except MoveError as err:
        assert err.category == CATEGORY_INVALID_INPUT
    else:  # pragma: no cover
        raise AssertionError("expected a MoveError")


def test_simulate_alternates_sides_and_reports_errors():
    engine = ChessEngine()
    ok = engine.simulate(None, ["e4", "e5", "Nf3"])
    assert ok["ok"] is True
    assert ok["applied"] == 3
    assert ok["board"].fen().startswith("rnbqkbnr/pppp1ppp/8/4p3/4P3/5N2")

    # Second move belongs to white again -> engine rejects it.
    bad = engine.simulate(None, ["e4", "Nf3"])
    assert bad["ok"] is False
    assert bad["applied"] == 1
    assert bad["steps"][1]["ok"] is False


def test_simulate_does_not_mutate_the_live_board():
    engine = ChessEngine()
    engine.simulate(None, ["d4", "d5"])
    assert engine.ply == 0


def test_bad_fen_is_rejected():
    engine = ChessEngine()
    try:
        engine.board_from_fen("this-is-not-a-fen")
    except MoveError as err:
        assert err.category == CATEGORY_INVALID_INPUT
    else:  # pragma: no cover
        raise AssertionError("expected a MoveError")


def test_last_move_survives_the_push():
    engine = ChessEngine()
    assert engine.last_move_san is None
    engine.apply_san("e4")
    assert engine.last_move_san == "e4"
    engine.apply_san("e5")
    assert engine.last_move_san == "e5"


def test_checkmate_is_detected():
    board = chess.Board("rnb1kbnr/pppp1ppp/8/4p3/6Pq/5P2/PPPPP2P/RNBQKBNR w KQkq - 1 3")
    result = evaluate_position(board, DrawConfig())
    assert result is not None
    assert result.reason == "checkmate"
    assert result.winner == "black"
    assert result.pgn_result == "0-1"


def test_fifty_move_rule_uses_the_configured_threshold():
    board = chess.Board("7k/8/8/8/8/8/8/K6R w - - 100 60")
    result = evaluate_position(board, DrawConfig(no_progress_moves=50))
    assert result is not None
    assert result.reason == "no_progress"


def test_resignation_result():
    result = resignation_result("white", "hopeless")
    assert result.winner == "black"
    assert result.pgn_result == "0-1"
