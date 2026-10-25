"""Configuration loading: api_key handling and the required config file."""

from __future__ import annotations

import textwrap

import pytest

from src.config import (
    DEFAULT_CONFIG_PATH,
    EXAMPLE_CONFIG_PATH,
    AppConfig,
    ConfigError,
    LlmProfile,
    load_config,
    normalise_reasoning,
)


def test_api_key_from_the_profile_wins_over_the_environment(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
    profile = LlmProfile(api_key="sk-from-file", api_key_env="OPENAI_API_KEY")

    assert profile.resolved_api_key == "sk-from-file"
    assert profile.configured is True


def test_api_key_falls_back_to_the_environment_variable(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
    profile = LlmProfile(api_key="", api_key_env="OPENAI_API_KEY")

    assert profile.resolved_api_key == "sk-from-env"


def test_profile_without_any_key_is_not_configured(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    profile = LlmProfile(api_key="", api_key_env="OPENAI_API_KEY")

    assert profile.resolved_api_key == ""
    assert profile.configured is False


def test_to_dict_never_leaks_the_raw_key():
    profile = LlmProfile(api_key="sk-secret")
    dumped = profile.to_dict()

    assert "sk-secret" not in repr(dumped)
    assert dumped["api_key"] == "from_config"


def test_missing_config_file_is_reported(tmp_path):
    with pytest.raises(ConfigError) as excinfo:
        load_config(tmp_path / "config.yaml", required=True)

    message = str(excinfo.value)
    assert "config.yaml" in message
    assert "config.example.yaml" in message


def test_the_shipped_template_exists_but_the_real_config_does_not():
    assert EXAMPLE_CONFIG_PATH.exists()
    assert DEFAULT_CONFIG_PATH.name == "config.yaml"


def test_load_config_reads_api_key_and_sections(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        textwrap.dedent(
            """
            llm_profiles:
              deepseek:
                base_url: "https://api.deepseek.com/v1"
                api_key: "sk-deepseek"
                model: "deepseek-chat"
            draw:
              max_game_moves: 120
            """
        ),
        encoding="utf-8",
    )

    cfg = load_config(path)
    profile = cfg.profile("deepseek")

    assert profile.name == "deepseek"
    assert profile.resolved_api_key == "sk-deepseek"
    assert profile.model == "deepseek-chat"
    assert cfg.draw.max_game_moves == 120


def test_profile_lookup_is_explicit_and_fails_on_unknown(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        textwrap.dedent(
            """
            llm_profiles:
              openai:
                api_key: "sk-openai"
            """
        ),
        encoding="utf-8",
    )
    cfg = load_config(path)

    assert cfg.has_profile("openai") is True
    assert cfg.is_profile_configured("openai") is True
    # An explicit, unknown name must fail loudly instead of inventing a profile.
    with pytest.raises(ConfigError):
        cfg.profile("nope")


def test_no_profile_name_is_special(tmp_path, monkeypatch):
    """A profile called `default` used to be magic.

    `OPENAI_BASE_URL` / `OPENAI_MODEL` were applied to whichever profile
    happened to be named `default`, and one was invented when none existed.
    The endpoints come from config.yaml alone now, so those variables must have
    no effect - and no profile may be conjured out of thin air.
    """

    monkeypatch.setenv("OPENAI_BASE_URL", "https://elsewhere.example/v1")
    monkeypatch.setenv("OPENAI_MODEL", "some-other-model")
    path = tmp_path / "config.yaml"
    path.write_text(
        'llm_profiles:\n  openai: {model: "gpt-4o-mini"}\n', encoding="utf-8"
    )

    cfg = load_config(path)

    assert sorted(cfg.llm_profiles) == ["openai"]
    assert cfg.profile("openai").base_url == "https://api.openai.com/v1"
    assert cfg.profile("openai").model == "gpt-4o-mini"


def test_reasoning_mode_is_normalised():
    assert normalise_reasoning("auto") == "auto"
    assert normalise_reasoning("ON") == "on"
    assert normalise_reasoning(True) == "on"
    assert normalise_reasoning("false") == "off"
    assert normalise_reasoning(False) == "off"
    assert normalise_reasoning(None) == "auto"
    assert normalise_reasoning("nonsense") == "auto"


def test_profile_reasoning_defaults_to_auto_and_can_be_turned_off(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        textwrap.dedent(
            """
            llm_profiles:
              default:
                model: "thinking-model"
              strict:
                model: "strict-model"
                reasoning: off
            """
        ),
        encoding="utf-8",
    )

    cfg = load_config(path)

    assert cfg.profile("default").reasoning_mode == "auto"
    assert cfg.profile("strict").reasoning_mode == "off"
    assert cfg.profile("strict").to_dict()["reasoning"] == "off"


@pytest.mark.parametrize(
    "body, expected",
    [
        ("- just\n- a list\n", "the top level must be a mapping"),
        ('llm_profiles: "openai"\n', "'llm_profiles' must be a mapping"),
        (
            "llm_profiles:\n  openai: sk-secret\n",
            "llm_profiles.openai must be a mapping",
        ),
        ("draw: [1, 2]\n", "'draw' must be a mapping"),
        ("loop: 20\n", "'loop' must be a mapping"),
            ("storage: 3\n", "'storage' must be a mapping"),
            ('seed: "abc"\n', "'seed' must be an integer"),
    ],
)
def test_malformed_config_is_reported_with_a_readable_message(tmp_path, body, expected):
    path = tmp_path / "config.yaml"
    path.write_text(body, encoding="utf-8")

    with pytest.raises(ConfigError) as excinfo:
        load_config(path)

    assert expected in str(excinfo.value)


def test_invalid_yaml_is_reported_as_a_config_error(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("llm_profiles: {openai: 'sk-\n", encoding="utf-8")

    with pytest.raises(ConfigError) as excinfo:
        load_config(path)

    assert "YAML" in str(excinfo.value)


def test_an_empty_config_file_is_valid_and_fully_defaulted(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("", encoding="utf-8")

    cfg = load_config(path)

    assert cfg.llm_profiles == {}
    assert cfg.loop.max_tool_turns == 20
    assert cfg.seed is None


def test_a_stale_server_section_is_ignored(tmp_path):
    # `server.allow_remote` is gone: the API is local-only with no opt-out, so
    # a config that still asks for remote access gets no such thing rather than
    # an error - the safe reading of an unknown key is to drop it.
    path = tmp_path / "config.yaml"
    path.write_text(
        'server:\n  allow_remote: "yes"\nllm_profiles:\n  a: {model: x}\n',
        encoding="utf-8",
    )

    cfg = load_config(path)

    assert "server" not in cfg.to_dict()
    assert list(cfg.llm_profiles) == ["a"]
