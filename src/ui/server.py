"""FastAPI application: REST endpoints plus a server-sent-event stream."""

from __future__ import annotations

import asyncio
import ipaddress
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
from src.game.storage import StoragePaths, list_games, load_game, new_game_id, setup_logging

LOGGER = logging.getLogger("src.ui.server")

STATIC_DIR = Path(__file__).resolve().parent / "static"
INDEX_PATH = STATIC_DIR / "index.html"


class GameContext:
    """Holds the mutable game and bridges sync game code with the event loop."""

    def __init__(self, controller: Optional[GameController], config: AppConfig, paths: StoragePaths) -> None:
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

    def emit_from(self, controller: GameController, event: Dict[str, Any]) -> None:
        """Publish an event only while ``controller`` still owns the board.

        A turn runs in a worker thread, so a game can be replaced (new game or
        load) while a model is still thinking. Without this guard the old game's
        ``tool_call`` / ``llm_delta`` / ``move`` events would appear in the new
        game's stream.
        """

        if controller is not self.controller:
            LOGGER.debug(
                "Dropping %s from a game that is no longer live", event.get("type")
            )
            return
        self.emit(event)

    def _deliver(self, event: Dict[str, Any]) -> None:
        """Fan an event out to every subscriber; runs on the event loop."""

        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except Exception:  # noqa: BLE001 - a broken client must not stop others
                LOGGER.debug("Dropping a subscriber that cannot take events")
                self._subscribers.discard(queue)

    def set_controller(self, controller: Optional[GameController]) -> None:
        previous = self.controller
        # Close the old game *before* swapping: its "closed" event is about the
        # game that is still current, so it must pass the emit_from() guard.
        if previous is not None and previous is not controller:
            try:
                previous.close()
            except Exception:  # noqa: BLE001
                LOGGER.exception("Failed to close the previous game cleanly")
        self.controller = controller
        # The controller may have been built before this context existed, in
        # which case it has no sink - adopt it so its events reach the UI.
        if controller is not None:
            controller.set_emit(
                lambda event, owner=controller: self.emit_from(owner, event)
            )

    # ------------------------------------------------------------------- turns
    def schedule_turn(self) -> None:
        if self.controller is None:
            return
        if self.task is not None and not self.task.done():
            # A drive loop is already running. It re-reads self.controller every
            # iteration, so a freshly swapped game is picked up there.
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
                ply_before = controller.engine.ply
                await asyncio.to_thread(controller.run_llm_turn)
                if controller is not self.controller:
                    # The game was replaced while that turn ran; this loop owns
                    # the serialisation, so serve the new game instead of
                    # leaving it waiting for a scheduled turn nobody will run.
                    # (No ply check here: the new game has its own count.)
                    continue
                if controller.finished or controller.current_player.kind != LLM:
                    break
                # A turn can also end without moving (a broken profile, a backend
                # that keeps erroring). The side to move has not changed, so
                # retrying it would spin forever; stop and leave the game as is.
                if controller.engine.ply == ply_before:
                    LOGGER.error(
                        "Turn for %s neither moved nor ended the game; stopping the driver.",
                        controller.side_to_move,
                    )
                    break
                # LLM vs LLM: keep going, but yield so events can flush.
                await asyncio.sleep(0)


class MoveRequest(BaseModel):
    move: str = Field(..., description="Move in SAN (e.g. Nf3) or UCI (e.g. g1f3).")


class ResignRequest(BaseModel):
    side: str = Field(..., description="'white' or 'black'.")
    # Optional; only humans can resign, and only for their own seat.
    reason: Optional[str] = Field(
        default=None, description="Short explanation shown in the game record."
    )


class NewGameRequest(BaseModel):
    # Each side is "human" or "llm:<profile-name>" (the profile must exist in
    # config.yaml). The UI always sends an explicit choice - there is no default.
    white: str
    black: str
    seed: Optional[int] = None


class LoadRequest(BaseModel):
    game_id: str
    # Optional override for either side; "human" or "llm:<profile-name>".
    # Left as null to keep the side as it was stored.
    white: Optional[str] = None
    black: Optional[str] = None


def create_app(
    config: AppConfig,
    paths: StoragePaths,
    controller: Optional[GameController] = None,
    context: Optional[GameContext] = None,
) -> FastAPI:
    ctx = context or GameContext(controller, config, paths)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        ctx.loop = asyncio.get_running_loop()
        if ctx.controller is not None:
            ctx.emit({"type": "hello", "game_id": ctx.controller.state.game_id})
        yield
        if ctx.controller is not None:
            try:
                ctx.controller.close()
            except Exception:  # noqa: BLE001
                LOGGER.exception("Failed to save the game on shutdown")

    app = FastAPI(title="chess-llm", version="0.1.0", lifespan=lifespan)
    app.state.ctx = ctx

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        """Refuse requests that did not address this machine by a local name.

        ``src.main`` never binds a non-loopback address, so nothing on the
        network can reach the socket in the first place. The one path left is
        DNS rebinding: a page the browser already has open resolves a name it
        controls to ``127.0.0.1`` and has the browser replay the requests,
        which then arrive from loopback with a foreign ``Host`` header. Only
        the header can tell those apart from the real UI - the peer address
        cannot, which is why it is not what gets checked here.
        """

        host = request.headers.get("host")
        if _addressed_to_this_machine(host):
            return await call_next(request)
        LOGGER.warning(
            "Rejected %s %s: Host %r is not a local name",
            request.method,
            request.url.path,
            host,
        )
        return JSONResponse(
            {
                "detail": (
                    "This server only answers requests addressed to this "
                    "machine (127.0.0.1 / localhost)."
                )
            },
            status_code=403,
        )

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        if not INDEX_PATH.exists():
            raise HTTPException(status_code=500, detail="index.html is missing")
        return HTMLResponse(INDEX_PATH.read_text(encoding="utf-8"))

    @app.get("/api/state")
    async def get_state() -> JSONResponse:
        # Nothing is running yet - the UI shows an empty "start a game" state.
        if ctx.controller is None:
            return JSONResponse({"game_id": None})
        return JSONResponse(ctx.controller.view())

    @app.get("/api/games")
    async def get_games() -> JSONResponse:
        return JSONResponse({"games": list_games(paths)})

    @app.get("/api/profiles")
    async def get_profiles() -> JSONResponse:
        return JSONResponse(
            {
                "profiles": [
                    {"name": name, "configured": profile.configured}
                    for name, profile in config.llm_profiles.items()
                ]
            }
        )

    @app.post("/api/move")
    async def post_move(payload: MoveRequest) -> JSONResponse:
        if ctx.controller is None:
            raise HTTPException(status_code=400, detail="No game is running.")
        result = ctx.controller.submit_human_move(payload.move)
        if not result.get("ok"):
            return JSONResponse(result, status_code=400)
        ctx.schedule_turn()
        return JSONResponse(result)

    @app.post("/api/resign")
    async def post_resign(payload: ResignRequest) -> JSONResponse:
        if ctx.controller is None:
            raise HTTPException(status_code=400, detail="No game is running.")
        side = payload.side.strip().lower()
        if side not in (WHITE, BLACK):
            raise HTTPException(status_code=400, detail="side must be 'white' or 'black'")
        reason = (payload.reason or "").strip() or None
        # The controller decides whether this seat may resign at all (human
        # seat, and in a hot-seat game the side that is to move).
        result = ctx.controller.resign(side, reason)
        if not result.get("ok"):
            return JSONResponse(result, status_code=400)
        return JSONResponse(result)

    @app.post("/api/new")
    async def post_new_game(payload: NewGameRequest) -> JSONResponse:
        white = _resolve_side(payload.white, config, WHITE)
        black = _resolve_side(payload.black, config, BLACK)
        state = GameState(
            game_id=new_game_id(),
            start_fen=chess.STARTING_FEN,
            white=white,
            black=black,
            seed=payload.seed if payload.seed is not None else config.seed,
        )
        setup_logging(
            state.game_id,
            paths,
            level=config.storage.log_level,
            to_console=config.storage.log_to_console,
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

        # For each side: an explicit override wins; otherwise the stored spec is
        # reused - but only if it is still playable. A stored LLM profile that
        # has since vanished or lost its key must be re-specified, never guessed.
        white = _effective_spec(payload.white, state.white, config, WHITE)
        black = _effective_spec(payload.black, state.black, config, BLACK)
        state.white = white
        state.black = black

        setup_logging(
            state.game_id,
            paths,
            level=config.storage.log_level,
            to_console=config.storage.log_to_console,
        )
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


def is_local_host(host: Optional[str]) -> bool:
    """True for loopback addresses and the name ``localhost``.

    Only used to read the name in a ``Host`` header, which arrives as ``name``
    or ``name:port`` - the caller strips the port first.
    """

    if not host:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.split("%")[0]).is_loopback
    except ValueError:
        return False


def _addressed_to_this_machine(host_header: Optional[str]) -> bool:
    """True when a ``Host`` header names this machine and nothing else.

    ``[::1]:8000`` and ``127.0.0.1:8000`` carry a port; a bare ``::1`` has
    colons of its own and must not be split on them.
    """

    if not host_header:
        return False
    name = host_header.strip().lower()
    if name.startswith("["):
        name = name[1:].split("]", 1)[0]
    elif name.count(":") == 1:
        name = name.split(":", 1)[0]
    return is_local_host(name)


def _parse_side(value: Optional[str]) -> tuple[str, Optional[str]]:
    """Turn ``"human"`` / ``"llm:name"`` into ``(kind, profile_name_or_None)``.

    ``None`` is returned unchanged so callers can treat "not provided" specially
    (the UI uses it for "keep the stored side" when loading a game).
    """

    if value is None:
        return (HUMAN, None)
    if value == HUMAN:
        return (HUMAN, None)
    if value.startswith(LLM + ":"):
        return (LLM, value[len(LLM) + 1 :])
    raise HTTPException(status_code=400, detail=f"Invalid side '{value}'.")


def _resolve_side(value: str, config: AppConfig, side: str) -> PlayerSpec:
    """Validate and build a :class:`PlayerSpec` from a UI side string."""

    kind, profile_name = _parse_side(value)
    if kind == HUMAN:
        return PlayerSpec.human()
    # kind == LLM
    if not profile_name:
        raise HTTPException(status_code=400, detail=f"{side}: an LLM side needs a profile name.")
    if not config.has_profile(profile_name):
        raise HTTPException(status_code=404, detail=f"{side}: unknown profile '{profile_name}'.")
    if not config.is_profile_configured(profile_name):
        raise HTTPException(
            status_code=400,
            detail=f"{side}: profile '{profile_name}' has no API key configured.",
        )
    return PlayerSpec.llm(profile_name)


def _effective_spec(
    override: Optional[str],
    stored: PlayerSpec,
    config: AppConfig,
    side: str,
) -> PlayerSpec:
    """Resolve a side spec when loading a saved game.

    ``override`` is ``None`` when the UI did not ask to change the seat: the
    stored spec is reused, provided it is still playable. Otherwise the override
    (human, or a named LLM profile that must exist and be configured) wins.
    """

    if override is None:
        if stored.kind == LLM and not config.is_profile_configured(stored.profile):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{side}: the stored profile '{stored.profile}' is no longer "
                    f"available; please specify who plays {side}."
                ),
            )
        return stored
    return _resolve_side(override, config, side)
