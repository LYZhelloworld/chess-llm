"""Game state: merged conversation, derived PGN comments, reasoning storage."""

from src.agent.llm_client import AssistantMessage, ToolCall, extract_reasoning
from src.agent.loop import ChatMemory
from src.game.state import GameState, PlayerSpec, chat_turns


def _state(white=None, black=None) -> GameState:
    return GameState(
        game_id="game_20260101_000000",
        white=PlayerSpec.llm("default", "m"),
        black=PlayerSpec.llm("default", "m"),
        white_chat_history=list(white or []),
        black_chat_history=list(black or []),
    )


def test_chat_turns_starts_a_group_at_every_user_message():
    turns = chat_turns(
        [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
            {"role": "tool", "content": "{}"},
            {"role": "user", "content": "c"},
        ]
    )
    assert [len(t) for t in turns] == [3, 1]


def test_conversation_interleaves_both_sides_and_skips_tools():
    state = _state(
        white=[
            {"role": "user", "content": "prompt"},
            {"role": "assistant", "content": "I open with e4.", "reasoning": "centre first"},
            {"role": "tool", "name": "make_move", "content": "{}"},
        ],
        black=[
            {"role": "user", "content": "prompt"},
            {"role": "assistant", "content": "", "reasoning": "silent thinking"},
        ],
    )

    entries = state.conversation()

    assert entries == [
        {"ply": 1, "side": "white", "content": "I open with e4.", "reasoning": "centre first"},
        {"ply": 2, "side": "black", "content": "", "reasoning": "silent thinking"},
    ]


def test_comments_come_from_the_chat_history_not_a_separate_list():
    state = _state(
        white=[
            {"role": "user", "content": "prompt"},
            {"role": "assistant", "content": "Let us open with e4."},
        ],
        black=[
            {"role": "user", "content": "prompt"},
            {"role": "assistant", "content": "Solid."},
            {"role": "assistant", "content": "I answer e5."},
        ],
    )

    assert state.comment_for_ply(0) == "Let us open with e4."
    assert state.comment_for_ply(1) == "Solid. | I answer e5."
    assert state.comment_for_ply(2) == ""
    assert "talks" not in state.to_dict()


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


def test_conversation_merges_every_message_of_one_turn():
    state = _state(
        black=[
            {"role": "user", "content": "prompt"},
            {"role": "assistant", "content": "", "reasoning": "checking the board"},
            {"role": "tool", "name": "get_legal_moves", "content": "{}"},
            {"role": "assistant", "content": "I play e5.", "reasoning": "it is solid"},
            {"role": "tool", "name": "make_move", "content": "{}"},
        ],
    )

    entries = state.conversation()

    assert len(entries) == 1
    assert entries[0]["ply"] == 2
    assert entries[0]["content"] == "I play e5."
    assert entries[0]["reasoning"] == "checking the board\n\n---\n\nit is solid"


def test_conversation_drops_a_repeated_remark():
    state = _state(
        white=[
            {"role": "user", "content": "prompt"},
            {"role": "assistant", "content": "Same sentence."},
            {"role": "tool", "name": "get_legal_moves", "content": "{}"},
            {"role": "assistant", "content": "Same sentence."},
        ],
    )

    entries = state.conversation()

    assert entries[0]["content"] == "Same sentence."


def test_conversation_skips_a_turn_that_said_nothing():
    state = _state(
        white=[
            {"role": "user", "content": "prompt"},
            {"role": "tool", "name": "make_move", "content": "{}"},
        ],
    )

    assert state.conversation() == []


def test_conversation_merges_every_message_of_one_turn():
    state = _state(
        black=[
            {"role": "user", "content": "prompt"},
            {"role": "assistant", "content": "", "reasoning": "checking the board"},
            {"role": "tool", "name": "get_legal_moves", "content": "{}"},
            {"role": "assistant", "content": "I play e5.", "reasoning": "it is solid"},
            {"role": "tool", "name": "make_move", "content": "{}"},
        ],
    )

    entries = state.conversation()

    assert len(entries) == 1
    assert entries[0]["ply"] == 2
    assert entries[0]["content"] == "I play e5."
    assert entries[0]["reasoning"] == "checking the board\n\n---\n\nit is solid"


def test_conversation_drops_a_repeated_remark():
    state = _state(
        white=[
            {"role": "user", "content": "prompt"},
            {"role": "assistant", "content": "Same sentence."},
            {"role": "tool", "name": "get_legal_moves", "content": "{}"},
            {"role": "assistant", "content": "Same sentence."},
        ],
    )

    entries = state.conversation()

    assert entries[0]["content"] == "Same sentence."


def test_conversation_skips_a_turn_that_said_nothing():
    state = _state(
        white=[
            {"role": "user", "content": "prompt"},
            {"role": "tool", "name": "make_move", "content": "{}"},
        ],
    )

    assert state.conversation() == []
