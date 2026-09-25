import json

from src.agent.llm_client import AssistantMessage, ToolCall
from src.config import AppConfig
from src.game.controller import GameController
from src.game.state import GameState, PlayerSpec
from src.game.storage import StoragePaths, load_game


class FakeClient:
    """Always answers with a single make_move tool call."""

    def __init__(self, move="Nf3", text="Attacking the centre."):
        self.move = move
        self.text = text
        self.calls = 0

    def chat(self, messages, tools=None):
        self.calls += 1
        return AssistantMessage(
            content=self.text,
            tool_calls=[
                ToolCall(id=f"call_{self.calls}", name="make_move", arguments=json.dumps({"move": self.move}))
            ],
        )

    def reasoning_enabled(self) -> bool:
        return True


def make_controller(tmp_path, white=None, black=None, moves=None):
    paths = StoragePaths(games_dir=tmp_path / "games", logs_dir=tmp_path / "logs").ensure()
    state = GameState(
        game_id="game_20260101_000000",
        white=white or PlayerSpec.human(),
        black=black or PlayerSpec.llm("default", "fake-model"),
        moves_san=list(moves or []),
    )
    return GameController(state, AppConfig(), paths), paths


def test_llm_turn_moves_and_records_the_transcript(tmp_path):
    controller, paths = make_controller(tmp_path)
    controller.submit_human_move("e4")

    fake = FakeClient(move="e5")
    controller._clients["black"] = fake
    outcome = controller.run_llm_turn()

    assert outcome["status"] == "moved"
    assert outcome["move"] == "e5"
    assert controller.state.moves_san == ["e4", "e5"]
    assert controller.state.comment_for_ply(1) == "Attacking the centre."
    said = controller.state.conversation()
    assert said == [{"ply": 2, "side": "black", "content": "Attacking the centre.", "reasoning": ""}]

    saved = load_game(controller.state.game_id, paths)
    assert saved.moves_san == ["e4", "e5"]
    assert any(m["role"] == "tool" for m in saved.black_chat_history)


def test_checkmate_finishes_and_saves_the_game(tmp_path):
    controller, paths = make_controller(
        tmp_path,
        white=PlayerSpec.llm("default", "fake"),
        black=PlayerSpec.llm("default", "fake"),
        moves=["f3", "e5", "g4"],
    )
    controller._clients["black"] = FakeClient(move="Qh4#")
    outcome = controller.run_llm_turn()

    assert outcome["status"] == "moved"
    assert controller.finished is True
    assert controller.state.result["reason"] == "checkmate"
    assert controller.state.result["winner"] == "black"
    assert controller.state.result["pgn_result"] == "0-1"

    saved = load_game(controller.state.game_id, paths)
    assert saved.finished is True
    assert saved.result["winner"] == "black"


def test_human_cannot_move_for_the_llm(tmp_path):
    controller, _ = make_controller(tmp_path, white=PlayerSpec.llm("default", "fake"))
    controller._clients["white"] = FakeClient(move="d4")
    result = controller.submit_human_move("e4")
    assert result["ok"] is False
    assert result["error"]["category"] == "illegal_state"


def test_human_vs_human_alternates_and_needs_no_llm(tmp_path):
    controller, _ = make_controller(tmp_path, white=PlayerSpec.human(), black=PlayerSpec.human())

    assert controller.state.mode == "human-vs-human"
    assert controller.step()["status"] == "waiting_human"

    first = controller.submit_human_move("e4")
    assert first["ok"] is True and first["side"] == "white"

    # Now it is black's seat: the same screen may play the reply.
    second = controller.submit_human_move("e5")
    assert second["ok"] is True and second["side"] == "black"

    assert controller.state.moves_san == ["e4", "e5"]
    assert controller._clients == {}  # no LLM client was ever built


def test_resignation_records_the_result(tmp_path):
    controller, paths = make_controller(tmp_path)
    controller.resign("white", "no time")
    assert controller.finished is True
    assert controller.state.result["reason"] == "resignation"
    assert controller.state.result["winner"] == "black"
    assert load_game(controller.state.game_id, paths).result["winner"] == "black"


def test_view_exposes_everything_the_ui_needs(tmp_path):
    controller, _ = make_controller(tmp_path)
    view = controller.view()
    for key in ("fen", "ascii", "turn", "legal_moves", "moves", "chats", "result", "mode"):
        assert key in view
    assert view["mode"] == "human-vs-llm"
    assert isinstance(view["legal_moves"][0]["san"], str)


def test_events_are_emitted(tmp_path):
    seen = []
    paths = StoragePaths(games_dir=tmp_path / "games", logs_dir=tmp_path / "logs").ensure()
    state = GameState(
        game_id="game_20260101_000001",
        white=PlayerSpec.human(),
        black=PlayerSpec.llm("default", "fake"),
    )
    controller = GameController(state, AppConfig(), paths, emit=seen.append)
    controller.submit_human_move("e4")
    assert any(event["type"] == "move" for event in seen)
