"""Configuration loading: api_key handling and the required config file."""

from __future__ import annotations

import textwrap

import pytest

from src.config import (
    DEFAULT_CONFIG_PATH,
    EXAMPLE_CONFIG_PATH,
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
            default_profile: deepseek
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
    profile = cfg.profile()

    assert profile.name == "deepseek"
    assert profile.resolved_api_key == "sk-deepseek"
    assert profile.model == "deepseek-chat"
    assert cfg.draw.max_game_moves == 120


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
