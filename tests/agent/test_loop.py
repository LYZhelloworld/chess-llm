import random

from src.agent.llm_client import AssistantMessage, ToolCall
from src.agent.loop import ChatMemory, run_llm_turn
from src.config import AppConfig, LoopConfig
from src.engine.board import ChessEngine


class ScriptedClient:
    """Returns canned assistant messages and records every request."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def chat(self, messages, tools=None):
        self.requests.append({"messages": list(messages), "tools": tools})
        if not self.responses:
            raise AssertionError("ScriptedClient ran out of responses")
        return self.responses.pop(0)


def text_only(content):
    return AssistantMessage(content=content, tool_calls=[])


def tool_call(name, arguments, content=None):
    return AssistantMessage(
        content=content,
        tool_calls=[ToolCall(id="call_1", name=name, arguments=arguments)],
    )


def test_agent_plays_a_move_after_exploring():
    engine = ChessEngine()
    memory = ChatMemory()
    client = ScriptedClient(
        tool_call("speak", '{"text": "Let me look at the board first."}'),
        tool_call("get_legal_moves", "{}"),
        tool_call("make_move", '{"move": "e4"}', content="I take the centre."),
    )
    outcome = run_llm_turn(client, engine, "white", memory, AppConfig(), rng=random.Random(0))

    assert outcome.move_san == "e4"
    assert outcome.resigned is False
    assert outcome.fallback_random is False
    # Only what went through `speak` is public; the plain text on the move
    # message stays private.
    assert outcome.spoken == ["Let me look at the board first."]
    assert engine.ply == 1

    roles = [m["role"] for m in memory.messages]
    assert roles == ["user", "assistant", "tool", "assistant", "tool", "assistant", "tool"]


def test_plain_model_text_is_never_spoken_and_emits_no_say_event():
    """The old behaviour - content shown in the chat - is what this replaces."""

    engine = ChessEngine()
    memory = ChatMemory()
    events = []
    client = ScriptedClient(
        text_only("I am thinking out loud here."),
        tool_call("make_move", '{"move": "d4"}', content="And another line nobody sees."),
    )

    outcome = run_llm_turn(
        client, engine, "white", memory, AppConfig(), emit=events.append, rng=random.Random(0)
    )

    assert outcome.spoken == []
    assert not [event for event in events if event["type"] == "say"]


def test_speak_emits_a_say_event_and_a_transcript_stamp():
    engine = ChessEngine()
    memory = ChatMemory()
    events = []
    stamped = []
    client = ScriptedClient(
        tool_call("speak", '{"text": "The centre is mine."}'),
        tool_call("make_move", '{"move": "e4"}'),
    )

    outcome = run_llm_turn(
        client,
        engine,
        "white",
        memory,
        AppConfig(),
        emit=events.append,
        rng=random.Random(0),
        stamp=lambda message, ply: stamped.append((message, ply)),
    )

    assert outcome.spoken == ["The centre is mine."]
    assert {"type": "say", "side": "white", "text": "The centre is mine."} in events
    assert len(stamped) == 1
    message, ply = stamped[0]
    assert ply == 1, "the remark belongs to the move it preceded"
    assert message["tool_calls"][0]["function"]["name"] == "speak"


def test_a_rejected_speak_is_not_announced():
    engine = ChessEngine()
    memory = ChatMemory()
    events = []
    client = ScriptedClient(
        tool_call("speak", '{"text": ""}'),
        tool_call("make_move", '{"move": "e4"}'),
    )

    outcome = run_llm_turn(
        client, engine, "white", memory, AppConfig(), emit=events.append, rng=random.Random(0)
    )

    assert outcome.spoken == []
    assert not [event for event in events if event["type"] == "say"]


def test_reply_stamps_never_reach_the_backend():
    """`seq` / `ply` / `spoken` are ours; an API would reject them."""

    engine = ChessEngine()
    memory = ChatMemory()
    client = ScriptedClient(
        tool_call("speak", '{"text": "Hello."}'),
        tool_call("make_move", '{"move": "e4"}'),
    )
    run_llm_turn(
        client,
        engine,
        "white",
        memory,
        AppConfig(),
        rng=random.Random(0),
        stamp=lambda message, ply: message.update({"seq": 7, "ply": ply}),
    )

    # Stored, so the conversation can be rebuilt...
    assert any(message.get("seq") == 7 for message in memory.messages)
    # ...but never sent: a backend would reject unknown message fields.
    for message in memory.window(10):
        assert "seq" not in message
        assert "ply" not in message
        assert "spoken" not in message


def test_opponent_speech_and_prompt_extras_reach_the_turn_message():
    engine = ChessEngine()
    memory = ChatMemory()
    client = ScriptedClient(
        text_only("Thinking out loud."),
        tool_call("make_move", '{"move": "e4"}', content="I play e4."),
    )
    run_llm_turn(
        client,
        engine,
        "white",
        memory,
        AppConfig(),
        rng=random.Random(0),
        opponent_speech="Your move, human.",
    )

    first_prompt = memory.messages[0]["content"]
    assert "Your move, human." in first_prompt
    assert "Material balance" in first_prompt
    assert "Legal moves" in first_prompt
    assert "e4" in first_prompt



def test_agent_is_nudged_when_it_never_calls_a_tool():
    engine = ChessEngine()
    memory = ChatMemory()
    client = ScriptedClient(*[text_only(f"thinking {i}") for i in range(3)])
    config = AppConfig()
    config.loop.max_tool_turns = 3

    outcome = run_llm_turn(client, engine, "white", memory, config, rng=random.Random(0))

    assert outcome.fallback_random is True
    assert outcome.move_san is not None
    assert engine.ply == 1
    assert memory.messages[-1]["content"].startswith("You ran out of exchanges")


def test_resignation_ends_the_turn_without_a_move():
    engine = ChessEngine()
    memory = ChatMemory()
    client = ScriptedClient(tool_call("resign", '{"reason": "lost material"}'))
    outcome = run_llm_turn(client, engine, "black", memory, AppConfig())

    assert outcome.resigned is True
    assert outcome.move_san is None
    assert outcome.resign_reason == "lost material"
    assert engine.ply == 0


def test_backend_failure_still_produces_a_move():
    class BrokenClient:
        def chat(self, messages, tools=None):
            raise RuntimeError("backend down")

    engine = ChessEngine()
    memory = ChatMemory()
    outcome = run_llm_turn(BrokenClient(), engine, "white", memory, AppConfig(), rng=random.Random(1))
    assert outcome.error
    assert outcome.fallback_random is True
    assert engine.ply == 1


def test_context_window_keeps_recent_turns_and_paired_tool_results():
    memory = ChatMemory()
    for i in range(6):
        memory.add({"role": "user", "content": f"prompt {i}"})
        memory.add(
            {
                "role": "assistant",
                "content": f"reply {i}",
                "tool_calls": [
                    {
                        "id": f"call_{i}",
                        "type": "function",
                        "function": {"name": "get_board", "arguments": "{}"},
                    }
                ],
            }
        )
        memory.add({"role": "tool", "tool_call_id": f"call_{i}", "name": "get_board", "content": "{}"})

    window = memory.window(2)
    assert window[0]["content"] == "prompt 4"
    assert [m["role"] for m in window] == [
        "user",
        "assistant",
        "tool",
        "user",
        "assistant",
        "tool",
    ]
    # Every tool result is preceded by the assistant call that produced it.
    for index, message in enumerate(window):
        if message["role"] == "tool":
            assert window[index - 1]["role"] == "assistant"


def test_window_never_drops_a_tool_call_and_keeps_its_result():
    memory = ChatMemory(
        [
            {"role": "user", "content": "first"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_x",
                        "type": "function",
                        "function": {"name": "make_move", "arguments": '{"move":"e4"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_x", "name": "make_move", "content": "{}"},
        ]
    )
    window = memory.window(1)
    assert len(window) == 3
    assert window[-1]["tool_call_id"] == "call_x"


def test_only_one_move_is_committed_when_several_are_requested():
    engine = ChessEngine()
    memory = ChatMemory()
    client = ScriptedClient(
        AssistantMessage(
            content="",
            tool_calls=[
                ToolCall(id="a", name="make_move", arguments='{"move": "e4"}'),
                ToolCall(id="b", name="make_move", arguments='{"move": "d4"}'),
            ],
        )
    )
    outcome = run_llm_turn(client, engine, "white", memory, AppConfig(), rng=random.Random(0))
    assert outcome.move_san == "e4"
    assert engine.ply == 1
    assert "ignored" not in json_of(memory, "b")


def json_of(memory, call_id):
    for message in memory.messages:
        if message.get("tool_call_id") == call_id:
            return message["content"]
    return ""


class RefusingClient:
    """Rejects any request that still carries a stored chain of thought."""

    def __init__(self, mode="auto"):
        self.mode = mode
        self.requests = []
        self._refused = False

    def chat(self, messages, tools=None):
        self.requests.append(list(messages))
        if any("reasoning" in m for m in messages):
            raise RuntimeError("400 unknown field 'reasoning'")
        return tool_call("make_move", '{"move": "e4"}')

    def reasoning_enabled(self):
        return self.mode != "off"

    def reasoning_refused(self):
        if self.mode != "auto" or self._refused:
            return False
        self._refused = True
        return True


def _memory_with_reasoning():
    return ChatMemory(
        [
            {"role": "user", "content": "It is your turn."},
            {"role": "assistant", "content": "I played something.", "reasoning": "deep thought"},
        ]
    )


def test_auto_mode_drops_the_chain_of_thought_after_a_refusal():
    engine = ChessEngine()
    memory = _memory_with_reasoning()
    client = RefusingClient()

    outcome = run_llm_turn(client, engine, "white", memory, AppConfig(), rng=random.Random(0))

    assert outcome.move_san == "e4"
    assert len(client.requests) == 2
    assert any("reasoning" in m for m in client.requests[0])
    assert not any("reasoning" in m for m in client.requests[1])
    # The transcript still holds it, even though it is no longer sent.
    assert memory.messages[1]["reasoning"] == "deep thought"


def test_reasoning_off_never_sends_it_and_does_not_retry():
    engine = ChessEngine()
    memory = _memory_with_reasoning()
    client = RefusingClient(mode="off")

    outcome = run_llm_turn(client, engine, "white", memory, AppConfig(), rng=random.Random(0))

    assert outcome.move_san == "e4"
    assert len(client.requests) == 1
    assert not any("reasoning" in m for m in client.requests[0])
def test_a_stale_fallback_switch_cannot_starve_a_turn():
    """`loop.fallback_random_move` is gone, and an old config file cannot bring
    it back.

    The switch used to be able to end a turn with no move at all, which left the
    same side to move and made the driver retry the turn immediately. A config
    that still says `false` is silently ignored now, because `from_dict` drops
    keys the dataclass no longer has.
    """

    assert "fallback_random_move" not in LoopConfig.__dataclass_fields__

    engine = ChessEngine()
    memory = ChatMemory()
    config = AppConfig()
    config.loop = LoopConfig.from_dict(
        {"max_tool_turns": 2, "fallback_random_move": False}
    )
    client = ScriptedClient(text_only("thinking"), text_only("still thinking"))

    outcome = run_llm_turn(client, engine, "white", memory, config, rng=random.Random(0))

    assert outcome.error is None
    assert outcome.fallback_random is True
    assert outcome.move_san is not None
    assert engine.ply == 1


# ------------------------------------------------------- closing remark at the end
# White mates with Rh8 on this board, so the move itself ends the game.
MATE_IN_ONE = "k7/8/1K6/8/8/8/8/7R w - - 0 1"


def test_the_model_gets_one_closing_remark_after_delivering_mate():
    engine = ChessEngine(MATE_IN_ONE)
    memory = ChatMemory()
    client = ScriptedClient(
        tool_call("make_move", '{"move": "Rh8"}'),
        tool_call("speak", '{"text": "Good game."}'),
    )

    outcome = run_llm_turn(client, engine, "white", memory, AppConfig())

    assert outcome.move_san == "Rh8#"
    assert outcome.spoken == ["Good game."]
    # One extra request, and it is prompted with the fact that the game is over.
    assert len(client.requests) == 2
    assert "The game is over" in client.requests[1]["messages"][-1]["content"]


def test_no_closing_remark_while_the_game_goes_on():
    engine = ChessEngine()
    memory = ChatMemory()
    client = ScriptedClient(tool_call("make_move", '{"move": "e4"}'))

    outcome = run_llm_turn(client, engine, "white", memory, AppConfig())

    assert outcome.move_san == "e4"
    assert len(client.requests) == 1
    assert outcome.spoken == []


def test_a_failed_closing_request_does_not_lose_the_move():
    """The game is already decided; a backend hiccup on the goodbye is noise."""

    engine = ChessEngine(MATE_IN_ONE)
    memory = ChatMemory()
    # Only one canned answer: the closing request raises.
    client = ScriptedClient(tool_call("make_move", '{"move": "Rh8"}'))

    outcome = run_llm_turn(client, engine, "white", memory, AppConfig())

    assert outcome.move_san == "Rh8#"
    assert outcome.resigned is False
    assert outcome.spoken == []
