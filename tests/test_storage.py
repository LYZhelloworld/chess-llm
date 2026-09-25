import json
import logging
from datetime import datetime

from src.config import AppConfig
from src.game.controller import GameController
from src.game.state import GameState, PlayerSpec
from src.game.storage import (
    MultilineFormatter,
    StoragePaths,
    build_pgn,
    list_games,
    load_game,
    new_game_id,
    save_game,
    timestamp_of,
)


def make_paths(tmp_path):
    return StoragePaths(games_dir=tmp_path / "games", logs_dir=tmp_path / "logs").ensure()


def make_state():
    state = GameState(
        game_id=new_game_id(datetime(2026, 9, 25, 19, 56, 46)),
        white=PlayerSpec.human(),
        black=PlayerSpec.llm("default", "gpt-4o-mini"),
        moves_san=["e4", "e5", "Nf3"],
        moves_uci=["e2e4", "e7e5", "g1f3"],
        seed=7,
    )
    # Remarks live in the chat history; the PGN comment is derived from it.
    state.white_chat_history = [
        {"role": "user", "content": "It is your turn."},
        {"role": "assistant", "content": "Let us open with e4."},
    ]
    state.black_chat_history = [{"role": "user", "content": "It is your turn."}]
    return state


def test_game_id_format_and_timestamp_extraction():
    game_id = new_game_id(datetime(2026, 9, 25, 19, 56, 46))
    assert game_id == "game_20260925_195646"
    assert timestamp_of(game_id) == "20260925_195646"
    assert timestamp_of("nonsense") != ""


def test_save_and_load_roundtrip(tmp_path):
    paths = make_paths(tmp_path)
    state = make_state()
    json_path, pgn_path = save_game(state, paths)

    assert json_path.name == "game_20260925_195646.json"
    assert pgn_path.name == "game_20260925_195646.pgn"

    loaded = load_game(state.game_id, paths)
    assert loaded.moves_san == ["e4", "e5", "Nf3"]
    assert loaded.black.is_llm
    assert loaded.black.model == "gpt-4o-mini"
    assert "talks" not in json.loads(json_path.read_text(encoding="utf-8"))
    assert loaded.comment_for_ply(0) == "Let us open with e4."
    assert loaded.black_chat_history[0]["role"] == "user"
    assert list_games(paths) == [state.game_id]


def test_pgn_contains_moves_headers_and_comments():
    state = make_state()
    state.result = {"winner": None, "reason": "agreement", "detail": "drawn", "pgn_result": "1/2-1/2"}
    pgn = build_pgn(state)
    assert "[White \"Human\"]" in pgn
    assert "[Black \"LLM:gpt-4o-mini\"]" in pgn
    assert "1. e4 " in pgn
    assert "1... e5 2. Nf3" in pgn
    assert "{ Let us open with e4. }" in pgn
    assert pgn.strip().endswith("1/2-1/2")


def test_controller_replays_the_saved_game(tmp_path):
    paths = make_paths(tmp_path)
    state = make_state()
    save_game(state, paths)

    loaded = load_game(state.game_id, paths)
    controller = GameController(loaded, AppConfig(), paths)
    assert controller.side_to_move == "black"
    assert len(controller.state.moves_san) == 3
    assert controller.engine.fen.startswith("rnbqkbnr/pppp1ppp/8/4p3/4P3/5N2")


def test_controller_persists_after_a_human_move(tmp_path):
    paths = make_paths(tmp_path)
    state = GameState(game_id=new_game_id(), white=PlayerSpec.human(), black=PlayerSpec.llm("default"))
    controller = GameController(state, AppConfig(), paths)

    result = controller.submit_human_move("d4")
    assert result["ok"] is True

    reloaded = load_game(state.game_id, paths)
    assert reloaded.moves_san == ["d4"]
    assert reloaded.moves_uci == ["d2d4"]


def test_human_move_rejection_is_reported(tmp_path):
    paths = make_paths(tmp_path)
    state = GameState(game_id=new_game_id(), white=PlayerSpec.human(), black=PlayerSpec.llm("default"))
    controller = GameController(state, AppConfig(), paths)
    result = controller.submit_human_move("Zzzz")
    assert result["ok"] is False
    assert result["error"]["category"] == "invalid_input"


def test_multiline_log_formatter_prefixes_every_line():
    formatter = MultilineFormatter()
    record = logging.LogRecord("src.test", logging.INFO, __file__, 1, "first\nsecond\nthird", None, None)
    formatted = formatter.format(record)
    lines = formatted.splitlines()
    assert len(lines) == 3
    assert lines[0].endswith("first")
    assert lines[1].endswith("second")
    assert lines[2].endswith("third")
    assert lines[0].split(" | ")[0] == lines[2].split(" | ")[0]


def test_json_record_is_serialisable(tmp_path):
    paths = make_paths(tmp_path)
    state = make_state()
    save_game(state, paths)
    raw = json.loads(paths.json_path(state.game_id).read_text(encoding="utf-8"))
    assert raw["mode"] == "human-vs-llm"
    assert raw["white_chat_history"][1]["content"] == "Let us open with e4."
    assert raw["black_chat_history"][0]["content"] == "It is your turn."
