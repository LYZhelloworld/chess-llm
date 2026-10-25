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
        ("server: on\n", "'server' must be a mapping"),
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
    assert cfg.server.allow_remote is False


def test_server_section_defaults_to_local_only_and_reads_the_flag(tmp_path):
    # The safe default matters: no authentication, and every turn costs money.
    assert AppConfig().server.allow_remote is False

    path = tmp_path / "config.yaml"
    path.write_text('server:\n  allow_remote: "yes"\n', encoding="utf-8")
    assert load_config(path).server.allow_remote is True

    path.write_text("server:\n  allow_remote: false\n", encoding="utf-8")
    assert load_config(path).server.allow_remote is False
