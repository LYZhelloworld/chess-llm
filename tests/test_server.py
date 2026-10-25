import asyncio
from types import SimpleNamespace

import chess

from src.config import AppConfig
from src.game.controller import GameController
from src.game.state import GameState, PlayerSpec
from src.game.storage import StoragePaths
from src.ui.server import GameContext


class ImmediateLoop:
    """Stand-in event loop: runs ``call_soon_threadsafe`` synchronously."""

    def call_soon_threadsafe(self, callback, *args):
        callback(*args)


def next_of_type(queue, event_type):
    """Pop events until one of ``event_type`` shows up (a move emits two)."""

    while True:
        event = queue.get_nowait()
        if event["type"] == event_type:
            return event


def make_context(tmp_path):
    paths = StoragePaths(games_dir=tmp_path / "games", logs_dir=tmp_path / "logs").ensure()
    state = GameState(
        game_id="game_20260101_000000",
        start_fen=chess.STARTING_FEN,
        white=PlayerSpec.human(),
        black=PlayerSpec.human(),
    )
    # Built exactly like src.main does: no emit, because the web layer does not
    # exist yet when the controller is created.
    controller = GameController(state, AppConfig(), paths)
    return GameContext(controller, AppConfig(), paths), controller


def test_context_binds_emit_to_a_controller_built_without_one(tmp_path):
    async def scenario():
        ctx, controller = make_context(tmp_path)
        ctx.loop = ImmediateLoop()
        queue = ctx.subscribe()

        result = controller.submit_human_move("e4")

        assert result["ok"] is True
        assert next_of_type(queue, "move")["move"] == "e4"

    asyncio.run(scenario())


def test_events_reach_every_subscriber(tmp_path):
    async def scenario():
        ctx, controller = make_context(tmp_path)
        ctx.loop = ImmediateLoop()
        first = ctx.subscribe()
        second = ctx.subscribe()

        controller.submit_human_move("d4")

        assert next_of_type(first, "move")["move"] == "d4"
        assert next_of_type(second, "move")["move"] == "d4"
        assert len(ctx._subscribers) == 2

        ctx.unsubscribe(first)
        parked = first.qsize()  # leftovers from the previous move stay put
        controller.submit_human_move("d5")
        assert next_of_type(second, "move")["move"] == "d5"
        assert first.qsize() == parked

    asyncio.run(scenario())


def test_set_controller_keeps_events_flowing(tmp_path):
    async def scenario():
        ctx, _ = make_context(tmp_path)
        ctx.loop = ImmediateLoop()
        queue = ctx.subscribe()

        paths = StoragePaths(games_dir=tmp_path / "games", logs_dir=tmp_path / "logs")
        state = GameState(
            game_id="game_20260101_000001",
            start_fen=chess.STARTING_FEN,
            white=PlayerSpec.human(),
            black=PlayerSpec.human(),
        )
        # Swapping the game closes the old one, which also emits - drain it.
        ctx.set_controller(GameController(state, AppConfig(), paths))
        next_of_type(queue, "closed")

        ctx.controller.submit_human_move("Nf3")
        assert next_of_type(queue, "move")["move"] == "Nf3"

    asyncio.run(scenario())


class StubTurn:
    """Minimal stand-in for a controller: only what ``_drive`` touches."""

    def __init__(self, kind="llm", stalled=False, stop_after=None):
        self.finished = False
        self.current_player = SimpleNamespace(kind=kind)
        self.stalled = stalled
        self.stop_after = stop_after
        self.turns = 0
        # _drive() compares the ply before and after a turn to notice a turn
        # that changed nothing.
        self.engine = SimpleNamespace(ply=0)

    @property
    def side_to_move(self):
        return "white"

    def set_emit(self, emit):
        pass

    def close(self):
        pass

    def run_llm_turn(self):
        self.turns += 1
        if not self.stalled:
            self.engine.ply += 1
        if self.stop_after is not None and self.turns >= self.stop_after:
            self.finished = True
        return {"status": "moved"}


def test_drive_keeps_playing_while_turns_change_the_board(tmp_path):
    """LLM vs LLM: every turn moves, so the driver hands over to the other side."""

    async def scenario():
        paths = StoragePaths(games_dir=tmp_path / "games", logs_dir=tmp_path / "logs")
        ctx = GameContext(None, AppConfig(), paths)
        both = StubTurn(stop_after=3)
        ctx.set_controller(both)
        ctx.schedule_turn()
        await ctx.task

        assert both.turns == 3

    asyncio.run(scenario())


def test_drive_stops_on_a_turn_that_changes_nothing(tmp_path):
    """A turn with no move and no result must not be retried.

    The side to move is unchanged, so the loop would start the same turn again
    immediately - forever, if the failure repeats (a backend that keeps
    erroring). That is the difference between one wasted call and a runaway.
    """

    async def scenario():
        paths = StoragePaths(games_dir=tmp_path / "games", logs_dir=tmp_path / "logs")
        ctx = GameContext(None, AppConfig(), paths)
        stuck = StubTurn(stalled=True)
        ctx.set_controller(stuck)
        ctx.schedule_turn()
        await ctx.task

        assert stuck.turns == 1

    asyncio.run(scenario())
