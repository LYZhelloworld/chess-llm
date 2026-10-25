"""HTTP API: profiles, empty state, new/load games and seat validation.

LLM-free integration paths (human sides) are exercised through the real app so
the request/response wiring is checked. The LLM-side *validation* (which would
otherwise kick off a real model call) is tested directly against the helper
functions so the suite never touches a network endpoint.
"""

from __future__ import annotations

import chess
import pytest
from fastapi.testclient import TestClient

from src.config import AppConfig, LlmProfile
from src.game.state import GameState, PlayerSpec
from src.game.storage import StoragePaths, save_game
from src.ui.server import _effective_spec, _resolve_side, create_app

WHITE = "white"
BLACK = "black"


def make_config() -> AppConfig:
    cfg = AppConfig()
    # A usable profile (has a key) and one that is present but unconfigured.
    cfg.llm_profiles["openai"] = LlmProfile(
        name="openai", api_key="sk-test", model="gpt-4o-mini", timeout=2.0, max_retries=0
    )
    cfg.llm_profiles["empty"] = LlmProfile(name="empty", model="x")
    return cfg


def make_client(tmp_path):
    paths = StoragePaths(
        games_dir=tmp_path / "games", logs_dir=tmp_path / "logs"
    ).ensure()
    cfg = make_config()
    app = create_app(config=cfg, paths=paths)
    # The Host header is checked, so the tests have to address the server the
    # way the browser does. TestClient's default base_url is "testserver".
    return TestClient(app, base_url="http://127.0.0.1"), paths


# ----------------------------------------------------------------- helpers
def test_resolve_side_human_is_always_valid():
    spec = _resolve_side("human", make_config(), WHITE)
    assert spec.kind == "human"


def test_resolve_side_llm_needs_an_existing_configured_profile():
    cfg = make_config()
    spec = _resolve_side("llm:openai", cfg, BLACK)
    assert spec.kind == "llm"
    assert spec.profile == "openai"

    # Unknown profile -> 404.
    with pytest.raises(Exception):
        _resolve_side("llm:ghost", cfg, BLACK)

    # Known but unconfigured profile -> 400.
    with pytest.raises(Exception):
        _resolve_side("llm:empty", cfg, BLACK)


def test_resolve_side_rejects_malformed_values():
    with pytest.raises(Exception):
        _resolve_side("llm:", make_config(), WHITE)  # empty profile name
    with pytest.raises(Exception):
        _resolve_side("robot", make_config(), WHITE)


def test_effective_spec_keeps_stored_side_when_still_playable():
    cfg = make_config()
    stored = PlayerSpec.human()
    assert _effective_spec(None, stored, cfg, WHITE) is stored

    stored_llm = PlayerSpec.llm("openai")
    assert _effective_spec(None, stored_llm, cfg, WHITE).profile == "openai"


def test_effective_spec_errors_when_stored_profile_is_gone():
    cfg = make_config()
    stored = PlayerSpec.llm("ghost")  # not in config
    with pytest.raises(Exception):
        _effective_spec(None, stored, cfg, WHITE)


def test_effective_spec_override_wins_and_is_validated():
    cfg = make_config()
    stored = PlayerSpec.llm("ghost")
    # Overriding to a real, configured profile rescues the game.
    spec = _effective_spec("llm:openai", stored, cfg, WHITE)
    assert spec.profile == "openai"


# ----------------------------------------------------------------- integration
def test_state_is_empty_before_any_game(tmp_path):
    client, _ = make_client(tmp_path)
    data = client.get("/api/state").json()
    assert data["game_id"] is None


def test_profiles_endpoint_lists_configured_flag(tmp_path):
    client, _ = make_client(tmp_path)
    profiles = {p["name"]: p for p in client.get("/api/profiles").json()["profiles"]}
    assert profiles["openai"]["configured"] is True
    assert profiles["empty"]["configured"] is False


def test_new_human_game_and_then_state_is_populated(tmp_path):
    client, _ = make_client(tmp_path)
    res = client.post("/api/new", json={"white": "human", "black": "human"})
    assert res.status_code == 200
    body = res.json()
    assert body["mode"] == "human-vs-human"
    assert client.get("/api/state").json()["game_id"] == body["game_id"]


def test_new_game_rejects_unknown_profile(tmp_path):
    client, _ = make_client(tmp_path)
    res = client.post("/api/new", json={"white": "human", "black": "llm:ghost"})
    assert res.status_code == 404


def test_new_game_rejects_unconfigured_profile(tmp_path):
    client, _ = make_client(tmp_path)
    res = client.post("/api/new", json={"white": "human", "black": "llm:empty"})
    assert res.status_code == 400


def _save_game(tmp_path, paths, white, black, game_id="game_20260101_000000"):
    state = GameState(
        game_id=game_id,
        start_fen=chess.STARTING_FEN,
        white=white,
        black=black,
    )
    save_game(state, paths)
    return game_id


def test_load_keeps_stored_seats(tmp_path):
    client, paths = make_client(tmp_path)
    gid = _save_game(tmp_path, paths, PlayerSpec.human(), PlayerSpec.human())
    res = client.post("/api/load", json={"game_id": gid})
    assert res.status_code == 200
    assert res.json()["mode"] == "human-vs-human"


def test_load_errors_when_stored_profile_is_unavailable(tmp_path):
    client, paths = make_client(tmp_path)
    # Stored as an LLM side whose profile no longer exists in config.
    gid = _save_game(tmp_path, paths, PlayerSpec.llm("ghost"), PlayerSpec.human())
    res = client.post("/api/load", json={"game_id": gid})
    assert res.status_code == 400
    assert "ghost" in res.json()["detail"]


# --------------------------------------------------------------------- resign
def test_resign_by_the_human_side_ends_the_game(tmp_path):
    client, _ = make_client(tmp_path)
    client.post("/api/new", json={"white": "human", "black": "human"})

    res = client.post("/api/resign", json={"side": "white"})

    assert res.status_code == 200
    body = res.json()
    assert body["resigned"] == "white"
    assert body["result"]["winner"] == "black"
    assert client.get("/api/state").json()["finished"] is True


def test_resign_accepts_an_optional_reason(tmp_path):
    client, _ = make_client(tmp_path)
    client.post("/api/new", json={"white": "human", "black": "human"})

    res = client.post("/api/resign", json={"side": "white", "reason": "no time"})

    assert res.status_code == 200
    assert res.json()["result"]["detail"] == "no time"


def test_resign_is_rejected_for_the_seat_that_is_not_to_move(tmp_path):
    client, _ = make_client(tmp_path)
    client.post("/api/new", json={"white": "human", "black": "human"})

    res = client.post("/api/resign", json={"side": "black"})

    assert res.status_code == 400
    assert res.json()["error"]["category"] == "illegal_state"


def test_resign_requires_a_running_game(tmp_path):
    client, _ = make_client(tmp_path)

    res = client.post("/api/resign", json={"side": "white"})

    assert res.status_code == 400
    assert "No game" in res.json()["detail"]


# ----------------------------------------------------------- local-only access
def test_a_request_addressed_to_another_name_is_refused(tmp_path):
    """DNS rebinding: the request arrives from loopback but is not for us.

    A page the browser already has open can resolve a name it controls to
    127.0.0.1 and have the browser replay its requests. The peer address is
    loopback for those, so the Host header is the only thing that separates
    them from the real UI - and the API has no authentication and spends the
    operator's credits.
    """

    client, _ = make_client(tmp_path)

    res = client.get("/api/state", headers={"host": "attacker.example"})

    assert res.status_code == 403
    assert "this machine" in res.json()["detail"]


def test_local_host_headers_are_served(tmp_path):
    client, _ = make_client(tmp_path)

    for host in ("127.0.0.1:8000", "localhost:8000", "[::1]:8000"):
        res = client.get("/api/state", headers={"host": host})
        assert res.status_code == 200, host
