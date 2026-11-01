"""Game state: the remark timeline, PGN comments, reasoning storage."""

import json

from src.agent.llm_client import AssistantMessage, ToolCall, extract_reasoning
from src.agent.loop import ChatMemory
from src.game.state import GameState, PlayerSpec, paragraphs_of, speech_entries


def _state(white=None, black=None) -> GameState:
    return GameState(
        game_id="game_20260101_000000",
        white=PlayerSpec.llm("default", "m"),
        black=PlayerSpec.llm("default", "m"),
        white_chat_history=list(white or []),
        black_chat_history=list(black or []),
    )


def _turn(*calls, content=None, seq=None, ply=None):
    """An assistant message carrying the given tool calls, as it is stored."""

    message = AssistantMessage(
        content=content,
        tool_calls=[
            ToolCall(id=f"call_{index}", name=name, arguments=arguments)
            for index, (name, arguments) in enumerate(calls)
        ],
    ).to_message()
    if seq is not None:
        message["seq"] = seq
    if ply is not None:
        message["ply"] = ply
    return message


def _speak(text, **kwargs):
    return _turn(("speak", json.dumps({"text": text})), **kwargs)


def test_speech_entries_read_speak_calls_and_ignore_everything_else():
    messages = [
        {"role": "user", "content": "It is your turn. You play white."},
        _turn(("speak", '{"text": "First."}'), content="private thinking", seq=1, ply=1),
        {"role": "tool", "name": "speak", "content": "{}"},
        _turn(("make_move", '{"move": "e4"}'), content="Now e4.", seq=2, ply=1),
    ]

    entries = speech_entries(messages, "white")

    assert [entry["text"] for entry in entries] == ["First."]
    assert entries[0]["kind"] == "speak"
    assert entries[0]["side"] == "white"
    assert entries[0]["seq"] == 1
    assert entries[0]["ply"] == 1


def test_a_human_remark_is_a_flagged_user_message():
    state = _state()
    state.speak("white", "Your move, machine.", 1)

    message = state.white_chat_history[0]
    assert message["role"] == "user"
    assert message["spoken"] is True
    assert message["seq"] == 1
    assert message["ply"] == 1
    assert [entry["kind"] for entry in state.remarks("white")] == ["say"]


def test_conversation_orders_both_sides_by_the_shared_counter():
    """The histories are separate lists; only `seq` can interleave them."""

    state = _state(
        white=[
            {"role": "user", "content": "your turn"},
            _speak("I open with e4.", seq=1, ply=1),
            # Typed while black was thinking, so it sorts before black's reply.
            {"role": "user", "content": "He is not even looking.",
             "spoken": True, "seq": 2, "ply": 3},
        ],
        black=[
            {"role": "user", "content": "your turn"},
            _speak("Bold.", seq=3, ply=2),
        ],
    )

    entries = state.conversation()

    assert [(entry["side"], entry["text"]) for entry in entries] == [
        ("white", "I open with e4."),
        ("white", "He is not even looking."),
        ("black", "Bold."),
    ]


def test_each_paragraph_becomes_its_own_bubble():
    state = _state(
        black=[
            {"role": "user", "content": "your turn"},
            _speak("Your king is exposed.\n\nAnd your rook is trapped.", seq=1, ply=2),
        ],
    )

    entries = state.conversation()

    assert [entry["text"] for entry in entries] == [
        "Your king is exposed.",
        "And your rook is trapped.",
    ]
    assert paragraphs_of("one\n\n\n  two  \n\n") == ["one", "two"]


def test_a_silent_game_has_an_empty_conversation():
    state = _state(
        white=[{"role": "user", "content": "your turn"}],
        black=[_turn(("make_move", '{"move": "e5"}'), seq=1, ply=2)],
    )

    assert state.conversation() == []


def test_hot_seat_seats_are_named_after_their_colour():
    """Both players share one screen, so "Human" would label both bubbles."""

    state = GameState(
        game_id="game_20260101_000000",
        white=PlayerSpec.human(),
        black=PlayerSpec.human(),
    )
    assert state.player_name("white") == "White"
    assert state.player_name("black") == "Black"


def test_a_single_human_keeps_the_generic_label():
    state = GameState(
        game_id="game_20260101_000000",
        white=PlayerSpec.human(),
        black=PlayerSpec.llm("default", "gpt-4o-mini"),
    )

    assert state.player_name("white") == "Human"
    assert state.player_name("black") == "LLM:gpt-4o-mini"


def test_a_rejected_speak_is_not_part_of_the_conversation():
    """An over-long call is refused by the tool, so it must not be shown."""

    state = _state(
        white=[
            {"role": "user", "content": "your turn"},
            _speak("x" * 501, seq=1, ply=1),
            _turn(("speak", "{not json"), seq=2, ply=1),
        ],
    )

    assert state.conversation() == []


def test_comments_come_from_the_remarks_of_the_side_that_moved():
    state = _state(
        white=[
            {"role": "user", "content": "your turn"},
            _speak("Let us open with e4.", seq=1, ply=1),
            _turn(("make_move", '{"move": "e4"}'), seq=2, ply=1),
        ],
        black=[
            {"role": "user", "content": "your turn"},
            _speak("Solid.", seq=3, ply=2),
            _speak("I answer e5.", seq=4, ply=2),
        ],
    )

    assert state.comment_for_ply(0) == "Let us open with e4."
    assert state.comment_for_ply(1) == "Solid. | I answer e5."
    assert state.comment_for_ply(2) == ""
    assert "talks" not in state.to_dict()


def test_a_remark_made_while_waiting_is_not_a_comment_on_the_opponents_move():
    """White types after moving: it belongs to white's next move, not black's."""

    state = _state()
    state.speak("white", "Hurry up.", 2)  # it is black's turn (ply 2)

    assert state.comment_for_ply(1) == "", "black did not say this"

    state.speak("white", "Told you.", 3)
    assert state.comment_for_ply(2) == "Told you."


def test_the_remarker_counter_continues_after_a_reload():
    state = _state(white=[_speak("Older.", seq=41, ply=1)])

    assert state.next_seq() == 42


def test_extract_reasoning_reads_the_common_field_names():
    class FakeMessage:
        model_extra = {"reasoning_content": "from extra"}

    assert extract_reasoning({"reasoning_content": "deepseek style"}) == "deepseek style"
    assert extract_reasoning({"reasoning": "openai style"}) == "openai style"
    assert extract_reasoning({"content": "no thinking"}) is None
    assert extract_reasoning(FakeMessage()) == "from extra"


def test_reasoning_is_stored_and_optionally_kept_out_of_the_request():
    memory = ChatMemory(
        [
            {
                "role": "assistant",
                "content": "playing e4",
                "reasoning": "the centre is good",
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "make_move", "arguments": '{"move":"e4"}'},
                    }
                ],
            }
        ]
    )

    assert memory.window(10)[0]["reasoning"] == "the centre is good"
    assert "reasoning" not in memory.window(10, keep_reasoning=False)[0]


def test_assistant_message_serialises_its_reasoning():
    message = AssistantMessage(
        content="e4",
        tool_calls=[ToolCall(id="c1", name="make_move", arguments='{"move":"e4"}')],
        reasoning="because",
    ).to_message()

    assert message["reasoning"] == "because"
    assert message["role"] == "assistant"
