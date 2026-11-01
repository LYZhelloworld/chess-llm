import json

from src.agent.llm_client import AssistantMessage, ToolCall
from src.config import AppConfig
from src.game.controller import GameController
from src.game.state import GameState, PlayerSpec
from src.game.storage import StoragePaths, load_game


class FakeClient:
    """Answers with an optional `speak` call plus a single make_move call."""

    def __init__(self, move="Nf3", text="Attacking the centre."):
        self.move = move
        self.text = text
        self.calls = 0
        self.seen_prompts = []
        # Optional callback run while the "model" is thinking, so a test can
        # type into the conversation mid-turn.
        self.during = None

    def chat(self, messages, tools=None):
        self.calls += 1
        self.seen_prompts.append(messages[-1]["content"] if messages else "")
        if self.during is not None:
            callback, self.during = self.during, None
            callback()
        calls = []
        if self.text:
            calls.append(
                ToolCall(
                    id=f"say_{self.calls}",
                    name="speak",
                    arguments=json.dumps({"text": self.text}),
                )
            )
        calls.append(
            ToolCall(
                id=f"call_{self.calls}",
                name="make_move",
                arguments=json.dumps({"move": self.move}),
            )
        )
        return AssistantMessage(content="", tool_calls=calls)

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
    assert len(said) == 1
    assert said[0]["side"] == "black"
    assert said[0]["kind"] == "speak"
    assert said[0]["text"] == "Attacking the centre."
    assert said[0]["ply"] == 2

    saved = load_game(controller.state.game_id, paths)
    assert saved.moves_san == ["e4", "e5"]
    assert any(m["role"] == "tool" for m in saved.black_chat_history)
    # The stamp survives a save/load round trip, so the conversation can be
    # rebuilt in the same order after a reload.
    assert saved.conversation()[0]["text"] == "Attacking the centre."


def test_controller_surfaces_the_opponents_remarks_once(tmp_path):
    controller, _ = make_controller(
        tmp_path,
        white=PlayerSpec.llm("default", "m1"),
        black=PlayerSpec.llm("default", "m2"),
    )

    white = FakeClient(move="e4", text="Opening the centre.")
    controller._clients["white"] = white
    controller.run_llm_turn()

    # Black is to move; its opponent (white) just spoke.
    assert controller._opponent_speech("black") == "Opening the centre."
    # Delivered once: asking again must not repeat the same heckle forever.
    assert controller._opponent_speech("black") is None
    # White has not yet heard anything from black.
    assert controller._opponent_speech("white") is None


def test_a_human_remark_is_stored_and_handed_to_the_model_next_turn(tmp_path):
    controller, paths = make_controller(tmp_path)

    result = controller.say("white", "Your opening is pathetic.")
    assert result["ok"] is True
    assert controller.state.conversation()[0]["text"] == "Your opening is pathetic."

    controller.submit_human_move("e4")
    fake = FakeClient(move="e5", text="We will see.")
    controller._clients["black"] = fake
    controller.run_llm_turn()

    prompt = fake.seen_prompts[0]
    assert "Your opening is pathetic." in prompt
    assert load_game(controller.state.game_id, paths).conversation()[0]["text"] == (
        "Your opening is pathetic."
    )


def test_a_remark_made_while_the_model_thinks_waits_for_the_next_turn(tmp_path):
    """A turn reads its prompt once; typing later must not interrupt it."""

    controller, _ = make_controller(tmp_path)
    controller.submit_human_move("e4")
    fake = FakeClient(move="e5")
    fake.during = lambda: controller.say("white", "Too slow.")
    controller._clients["black"] = fake

    controller.run_llm_turn()

    assert "Too slow." not in fake.seen_prompts[0]
    # It is not lost: the next prompt of that side carries it.
    controller.submit_human_move("Nf3")
    second = FakeClient(move="Nc6")
    controller._clients["black"] = second
    controller.run_llm_turn()
    assert "Too slow." in second.seen_prompts[0]


def test_only_a_human_seat_can_speak(tmp_path):
    controller, _ = make_controller(tmp_path)  # white human, black LLM

    refused = controller.say("black", "I am a model.")
    assert refused["ok"] is False
    assert refused["error"]["category"] == "illegal_state"
    assert controller.state.conversation() == []


def test_a_human_may_say_goodbye_after_the_game_ends(tmp_path):
    controller, _ = make_controller(tmp_path)
    controller.resign("white", "done")

    result = controller.say("white", "Good game.")

    assert result["ok"] is True
    said = controller.state.conversation()
    assert [(entry["side"], entry["text"]) for entry in said] == [
        ("white", "Good game.")
    ]
    # A remark made after the final move belongs to no ply, so it never turns
    # into a PGN comment on the last move.
    assert controller.state.white_chat_history[-1]["ply"] == 0


def test_a_model_only_matchup_still_has_nobody_to_speak_after_the_game(tmp_path):
    controller, _ = make_controller(
        tmp_path,
        white=PlayerSpec.llm("default", "m1"),
        black=PlayerSpec.llm("default", "m2"),
    )
    controller.state.finished = True

    assert controller.say_side is None


def test_speaking_rejects_empty_and_over_long_text(tmp_path):
    controller, _ = make_controller(tmp_path)

    assert controller.say("white", "   ")["error"]["category"] == "invalid_input"
    assert controller.say("white", "x" * 501)["error"]["category"] == "invalid_input"
    assert controller.state.conversation() == []


def test_hot_seat_speech_belongs_to_the_side_to_move(tmp_path):
    controller, _ = make_controller(
        tmp_path, white=PlayerSpec.human(), black=PlayerSpec.human()
    )

    assert controller.say_side == "white"
    assert controller.say("white", "Opening.")["ok"] is True
    refused = controller.say("black", "Wait your turn.")
    assert refused["ok"] is False
    assert "white" in refused["error"]["message"]

    controller.submit_human_move("e4")
    assert controller.say_side == "black"
    assert controller.say("black", "Fine.")["ok"] is True


def test_an_llm_only_game_offers_no_place_to_speak(tmp_path):
    controller, _ = make_controller(
        tmp_path,
        white=PlayerSpec.llm("default", "m1"),
        black=PlayerSpec.llm("default", "m2"),
    )

    assert controller.say_side is None
    assert controller.view()["say_side"] is None


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


class ResigningClient:
    """Answers with a resign tool call instead of a move."""

    def __init__(self, reason=None):
        self.reason = reason
        self.calls = 0

    def chat(self, messages, tools=None):
        self.calls += 1
        arguments = {} if self.reason is None else {"reason": self.reason}
        return AssistantMessage(
            content="This is lost.",
            tool_calls=[
                ToolCall(id=f"call_{self.calls}", name="resign", arguments=json.dumps(arguments))
            ],
        )

    def reasoning_enabled(self) -> bool:
        return True


def test_llm_can_resign_through_its_own_tool(tmp_path):
    controller, paths = make_controller(tmp_path)
    controller.submit_human_move("e4")

    controller._clients["black"] = ResigningClient("queen is gone")
    outcome = controller.run_llm_turn()

    assert outcome["status"] == "resigned"
    assert outcome["side"] == "black"
    assert controller.finished is True
    assert controller.state.result["reason"] == "resignation"
    assert controller.state.result["winner"] == "white"
    assert controller.state.result["detail"] == "queen is gone"
    assert load_game(controller.state.game_id, paths).result["winner"] == "white"


def test_resignation_is_refused_for_an_llm_seat(tmp_path):
    # White is the human here, black is the model: an API caller must not be
    # able to make the model resign.
    controller, _ = make_controller(tmp_path)

    result = controller.resign("black")

    assert result["ok"] is False
    assert result["error"]["category"] == "illegal_state"
    assert controller.finished is False


def test_hot_seat_only_allows_the_side_to_move_to_resign(tmp_path):
    controller, _ = make_controller(
        tmp_path, white=PlayerSpec.human(), black=PlayerSpec.human()
    )

    refused = controller.resign("black")
    assert refused["ok"] is False
    assert "white" in refused["error"]["message"]

    accepted = controller.resign("white", "   ")
    assert accepted["ok"] is True
    assert controller.state.result["winner"] == "black"
    assert controller.state.result["detail"] == "White resigned."


def test_resignation_rejects_bad_input(tmp_path):
    controller, _ = make_controller(tmp_path)

    assert controller.resign("green")["error"]["category"] == "invalid_input"
    assert (
        controller.resign("white", reason=object())["error"]["category"]
        == "invalid_input"
    )
    assert controller.finished is False


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


def test_hot_seat_players_can_both_say_goodbye(tmp_path):
    """After the last move there is no turn to own the floor any more."""

    controller, _ = make_controller(
        tmp_path, white=PlayerSpec.human(), black=PlayerSpec.human()
    )
    controller.state.finished = True

    assert controller.say("white", "Good game.")["ok"] is True
    assert controller.say("black", "You too.")["ok"] is True
