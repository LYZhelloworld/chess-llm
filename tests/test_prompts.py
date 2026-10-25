from src.engine.board import ChessEngine
from src.prompts import turn_prompt


def test_turn_prompt_includes_material_legal_and_opponent():
    engine = ChessEngine()
    text = turn_prompt(engine, "white", opponent_speech="Your move, coward.")

    assert "Material balance" in text
    # Starting material: 8P + 2N + 2B + 2R + Q = 8 + 6 + 6 + 10 + 9 = 39 each.
    assert "White 39 - Black 39" in text
    assert "Legal moves" in text
    assert "e4" in text
    assert 'Opponent\'s last spoken remark: "Your move, coward."' in text


def test_turn_prompt_reports_no_opponent_speech():
    engine = ChessEngine()
    text = turn_prompt(engine, "white")

    assert "Opponent has not spoken yet this game." in text


def test_turn_prompt_material_balance_reflects_a_capture():
    engine = ChessEngine()
    for san in ["e4", "e5", "Nf3", "Nc6", "Bb5", "a6", "Bxc6"]:
        engine.apply_san(san)

    text = turn_prompt(engine, "white")

    # White captured a knight (value 3); black is now down to 36.
    assert "White 39 - Black 36" in text
    assert "white is ahead by 3" in text


def test_turn_prompt_material_balance_from_black_perspective():
    engine = ChessEngine()
    for san in ["e4", "e5", "Nf3", "Nc6", "Bb5", "a6", "Bxc6"]:
        engine.apply_san(san)

    text = turn_prompt(engine, "black")

    # Black is behind, so from black's perspective it says "you are behind".
    assert "you are behind by 3" in text


def test_turn_prompt_lists_every_legal_move():
    engine = ChessEngine()
    text = turn_prompt(engine, "white")

    for san in engine.legal_moves_san():
        assert san in text


def test_turn_prompt_states_the_piece_values_once():
    text = turn_prompt(ChessEngine(), "white")

    # The label above the reading carries the weights; the reading itself must
    # not repeat them, which it used to do in a trailing "Values: ..." sentence.
    assert text.count("P=1") == 1
    assert "Values:" not in text
