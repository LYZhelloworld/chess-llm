"""FastAPI application: REST endpoints plus a server-sent-event stream."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, Optional

import chess
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from src.config import AppConfig, BLACK, HUMAN, LLM, WHITE
from src.game.controller import GameController
from src.game.state import GameState, PlayerSpec
from src.game.storage import StoragePaths, list_games, load_game, new_game_id

LOGGER = logging.getLogger("src.ui.server")

STATIC_DIR = Path(__file__).resolve().parent / "static"
INDEX_PATH = STATIC_DIR / "index.html"


class GameContext:
    """Holds the mutable game and bridges sync game code with the event loop."""

    def __init__(self, controller: GameController, config: AppConfig, paths: StoragePaths) -> None:
        self.config = config
        self.paths = paths
        self.lock = asyncio.Lock()
        self.task: Optional[asyncio.Task] = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self._subscribers: set[asyncio.Queue] = set()
        self.controller: Optional[GameController] = None
        self.set_controller(controller)

    # ------------------------------------------------------------------ events
    def subscribe(self) -> "asyncio.Queue":
        """Create a queue that receives every future event (one per client)."""

        queue: asyncio.Queue = asyncio.Queue()
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: "asyncio.Queue") -> None:
        self._subscribers.discard(queue)

    def emit(self, event: Dict[str, Any]) -> None:
        LOGGER.debug("event: %s", json.dumps(event, ensure_ascii=False)[:400])
        loop = self.loop
        if loop is None:
            return
        try:
            loop.call_soon_threadsafe(self._deliver, event)
        except RuntimeError:  # loop already closed
            LOGGER.debug("Event loop closed, dropping event %s", event.get("type"))

    def _deliver(self, event: Dict[str, Any]) -> None:
        """Fan an event out to every subscriber; runs on the event loop."""

        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except Exception:  # noqa: BLE001 - a broken client must not stop others
                LOGGER.debug("Dropping a subscriber that cannot take events")
                self._subscribers.discard(queue)

    def set_controller(self, controller: GameController) -> None:
        previous = self.controller
        self.controller = controller
        # The controller may have been built before this context existed, in
        # which case it has no sink - adopt it so its events reach the UI.
        if controller is not None:
            controller.set_emit(self.emit)
        if previous is not None and previous is not controller:
            try:
                previous.close()
            except Exception:  # noqa: BLE001
                LOGGER.exception("Failed to close the previous game cleanly")

    # ------------------------------------------------------------------- turns
    def schedule_turn(self) -> None:
        if self.task is not None and not self.task.done():
            return
        self.task = asyncio.create_task(self._drive())

    async def _drive(self) -> None:
        async with self.lock:
            while True:
                controller = self.controller
                if controller is None or controller.finished:
                    break
                if controller.current_player.kind != LLM:
                    break
                await asyncio.to_thread(controller.run_llm_turn)
                if controller.current_player.kind == LLM and not controller.finished:
                    # LLM vs LLM: keep going, but yield so events can flush.
                    await asyncio.sleep(0)
                    continue
                break


class MoveRequest(BaseModel):
    move: str = Field(..., description="Move in SAN (e.g. Nf3) or UCI (e.g. g1f3).")


class ResignRequest(BaseModel):
    side: str = Field(..., description="'white' or 'black'.")
    reason: str = ""


class NewGameRequest(BaseModel):
    white: str = HUMAN
    black: str = LLM
    white_profile: Optional[str] = None
    black_profile: Optional[str] = None
    seed: Optional[int] = None


class LoadRequest(BaseModel):
    game_id: str


def create_app(
    controller: GameController,
    config: AppConfig,
    paths: StoragePaths,
    context: Optional[GameContext] = None,
) -> FastAPI:
    ctx = context or GameContext(controller, config, paths)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        ctx.loop = asyncio.get_running_loop()
        ctx.emit({"type": "hello", "game_id": ctx.controller.state.game_id})
        yield
        try:
            ctx.controller.close()
        except Exception:  # noqa: BLE001
            LOGGER.exception("Failed to save the game on shutdown")

    app = FastAPI(title="chess-llm", version="0.1.0", lifespan=lifespan)
    app.state.ctx = ctx

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        if not INDEX_PATH.exists():
            raise HTTPException(status_code=500, detail="index.html is missing")
        return HTMLResponse(INDEX_PATH.read_text(encoding="utf-8"))

    @app.get("/api/state")
    async def get_state() -> JSONResponse:
        return JSONResponse(ctx.controller.view())

    @app.get("/api/games")
    async def get_games() -> JSONResponse:
        return JSONResponse({"games": list_games(paths)})

    @app.post("/api/move")
    async def post_move(payload: MoveRequest) -> JSONResponse:
        result = ctx.controller.submit_human_move(payload.move)
        if not result.get("ok"):
            return JSONResponse(result, status_code=400)
        ctx.schedule_turn()
        return JSONResponse(result)

    @app.post("/api/resign")
    async def post_resign(payload: ResignRequest) -> JSONResponse:
        side = payload.side.lower()
        if side not in (WHITE, BLACK):
            raise HTTPException(status_code=400, detail="side must be 'white' or 'black'")
        result = ctx.controller.resign(side, payload.reason or None)
        if not result.get("ok"):
            return JSONResponse(result, status_code=400)
        return JSONResponse(result)

    @app.post("/api/new")
    async def post_new_game(payload: NewGameRequest) -> JSONResponse:
        state = _build_state(
            payload.white,
            payload.black,
            payload.white_profile,
            payload.black_profile,
            config,
            payload.seed if payload.seed is not None else config.seed,
        )
        new_controller = GameController(state, config, paths, emit=ctx.emit)
        ctx.set_controller(new_controller)
        ctx.emit({"type": "game_started", "game_id": state.game_id, "mode": state.mode})
        ctx.schedule_turn()
        return JSONResponse(new_controller.view())

    @app.post("/api/load")
    async def post_load(payload: LoadRequest) -> JSONResponse:
        game_id = payload.game_id.strip()
        if game_id.endswith(".json"):
            game_id = game_id[: -len(".json")]
        try:
            state = load_game(game_id, paths)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        new_controller = GameController(state, config, paths, emit=ctx.emit)
        ctx.set_controller(new_controller)
        ctx.emit({"type": "game_loaded", "game_id": state.game_id})
        ctx.schedule_turn()
        return JSONResponse(new_controller.view())

    @app.get("/api/events")
    async def events(request: Request) -> StreamingResponse:
        async def stream():
            queue = ctx.subscribe()
            try:
                yield "data: " + json.dumps({"type": "connected"}) + "\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=15.0)
                    except asyncio.TimeoutError:
                        yield ": keep-alive\n\n"
                        continue
                    yield "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
            finally:
                ctx.unsubscribe(queue)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return app


def _build_state(
    white_kind: str,
    black_kind: str,
    white_profile: Optional[str],
    black_profile: Optional[str],
    config: AppConfig,
    seed: Optional[int],
) -> GameState:
    white = (
        PlayerSpec.llm(white_profile or config.default_profile)
        if white_kind == LLM
        else PlayerSpec.human()
    )
    black = (
        PlayerSpec.llm(black_profile or config.default_profile)
        if black_kind == LLM
        else PlayerSpec.human()
    )
    return GameState(
        game_id=new_game_id(),
        start_fen=chess.STARTING_FEN,
        white=white,
        black=black,
        seed=seed,
    )
