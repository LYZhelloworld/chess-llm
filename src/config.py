"""Application configuration.

Configuration is loaded from ``config.yaml`` and then refined by environment
variables and command line arguments. ``config.yaml`` is not shipped with the
repository: copy ``config.example.yaml`` to ``config.yaml`` first, otherwise
the application refuses to start.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"
EXAMPLE_CONFIG_PATH = PROJECT_ROOT / "config.example.yaml"


class ConfigError(RuntimeError):
    """Raised when ``config.yaml`` is missing or cannot be read."""

WHITE = "white"
BLACK = "black"
HUMAN = "human"
LLM = "llm"


# How a profile treats the chain of thought a reasoning model returns:
#   auto - send it back with the history, stop if the backend refuses it
#   on   - always send it back
#   off  - never send it back (it is still stored in the game JSON)
REASONING_MODES = ("auto", "on", "off")

# Whether responses are streamed token by token to the UI:
#   auto - stream when the backend supports it, fall back if it fails
#   on   - always stream
#   off  - never stream (wait for the full reply)
STREAM_MODES = ("auto", "on", "off")


def normalise_reasoning(value: Any) -> str:
    """Coerce ``true`` / ``"yes"`` / ``"auto"`` / ... into a reasoning mode."""

    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("on", "true", "yes", "1"):
            return "on"
        if text in ("off", "false", "no", "0"):
            return "off"
        return "auto"
    if value is True:
        return "on"
    if value is False:
        return "off"
    return "auto"


def normalise_stream(value: Any) -> str:
    """Coerce ``true`` / ``"yes"`` / ``"auto"`` / ... into a stream mode."""

    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("on", "true", "yes", "1"):
            return "on"
        if text in ("off", "false", "no", "0"):
            return "off"
        return "auto"
    if value is True:
        return "on"
    if value is False:
        return "off"
    return "auto"


@dataclass
class LlmProfile:
    """Connection parameters for one OpenAI-compatible endpoint."""

    name: str = "default"
    base_url: str = "https://api.openai.com/v1"
    # Direct key. When empty the key is read from the ``api_key_env`` variable.
    api_key: str = ""
    # Fallback: name of the environment variable holding the key.
    api_key_env: str = "OPENAI_API_KEY"
    model: str = "gpt-4o-mini"
    temperature: float = 0.7
    top_p: float = 1.0
    max_tokens: int = 2048
    timeout: float = 120.0
    max_retries: int = 2
    retry_backoff: float = 2.0
    # "auto" | "on" | "off" - see REASONING_MODES.
    reasoning: str = "auto"
    # "auto" | "on" | "off" - see STREAM_MODES.
    stream: str = "auto"

    @property
    def resolved_api_key(self) -> str:
        """The key to use: ``api_key`` from the file, else the env variable."""

        return self.api_key or os.environ.get(self.api_key_env, "")

    @property
    def configured(self) -> bool:
        return bool(self.resolved_api_key)

    @property
    def reasoning_mode(self) -> str:
        return normalise_reasoning(self.reasoning)

    @property
    def stream_mode(self) -> str:
        return normalise_stream(self.stream)

    def override(self, **kwargs: Any) -> "LlmProfile":
        values = {k: v for k, v in kwargs.items() if v is not None}
        return replace(self, **values)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "base_url": self.base_url,
            # Never serialise the raw key; only report where it came from.
            "api_key": "from_config" if self.api_key else ("env:" + self.api_key_env if self.configured else ""),
            "api_key_env": self.api_key_env,
            "model": self.model,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "max_tokens": self.max_tokens,
            "timeout": self.timeout,
            "max_retries": self.max_retries,
            "retry_backoff": self.retry_backoff,
            "reasoning": self.reasoning_mode,
            "stream": self.stream_mode,
        }

    @classmethod
    def from_dict(cls, name: str, data: Dict[str, Any]) -> "LlmProfile":
        fields = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        fields["reasoning"] = normalise_reasoning(fields.get("reasoning", "auto"))
        fields["stream"] = normalise_stream(fields.get("stream", "auto"))
        return cls(name=name, **fields)


@dataclass
class DrawConfig:
    """Thresholds used by the rule engine to declare a draw."""

    repetition_count: int = 3
    no_progress_moves: int = 50
    max_game_moves: int = 200
    insufficient_material: bool = True
    stalemate: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DrawConfig":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class LoopConfig:
    """Parameters of the LLM agent loop."""

    max_tool_turns: int = 20
    context_turns: int = 20
    fallback_random_move: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "LoopConfig":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class StorageConfig:
    """Where games and logs are written."""

    games_dir: str = "games"
    logs_dir: str = "logs"
    autosave: bool = True
    log_level: str = "INFO"
    log_to_console: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "StorageConfig":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class AppConfig:
    llm_profiles: Dict[str, LlmProfile] = field(default_factory=dict)
    default_profile: str = "default"
    draw: DrawConfig = field(default_factory=DrawConfig)
    loop: LoopConfig = field(default_factory=LoopConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    seed: Optional[int] = None

    def profile(self, name: Optional[str] = None) -> LlmProfile:
        key = name or self.default_profile
        if key in self.llm_profiles:
            return self.llm_profiles[key]
        base = self.llm_profiles.get(self.default_profile, LlmProfile())
        return base.override(name=key)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "default_profile": self.default_profile,
            "llm_profiles": {k: v.to_dict() for k, v in self.llm_profiles.items()},
            "draw": self.draw.to_dict(),
            "loop": self.loop.to_dict(),
            "storage": self.storage.to_dict(),
            "seed": self.seed,
        }


def missing_config_message(cfg_path: Path) -> str:
    """Human readable instructions printed when ``config.yaml`` is absent."""

    return (
        f"Configuration file not found: {cfg_path}\n"
        "Create it before starting, e.g.:\n"
        f"  cp {EXAMPLE_CONFIG_PATH.name} {Path(cfg_path).name}\n"
        f"(or: copy {EXAMPLE_CONFIG_PATH} -> {cfg_path})\n"
        "Then edit it: set llm_profiles.<name>.api_key (or api_key_env) and model."
    )


def load_config(path: Optional[Path | str] = None, required: bool = True) -> AppConfig:
    """Load configuration from YAML.

    When ``required`` is true and the file does not exist a :class:`ConfigError`
    is raised; callers that only need the built-in defaults pass
    ``required=False``.
    """

    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    data: Dict[str, Any] = {}
    if cfg_path.exists():
        with cfg_path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    elif required:
        raise ConfigError(missing_config_message(cfg_path))

    profiles: Dict[str, LlmProfile] = {}
    raw_profiles = data.get("llm_profiles") or {}
    for name, raw in raw_profiles.items():
        profiles[name] = LlmProfile.from_dict(str(name), raw or {})
    if not profiles:
        profiles["default"] = LlmProfile()

    cfg = AppConfig(
        llm_profiles=profiles,
        default_profile=str(data.get("default_profile", "default")),
        draw=DrawConfig.from_dict(data.get("draw") or {}),
        loop=LoopConfig.from_dict(data.get("loop") or {}),
        storage=StorageConfig.from_dict(data.get("storage") or {}),
        seed=data.get("seed"),
    )

    # Environment convenience overrides for the default profile.
    env_base_url = os.environ.get("OPENAI_BASE_URL")
    env_model = os.environ.get("OPENAI_MODEL")
    if env_base_url or env_model:
        default = cfg.llm_profiles.get(cfg.default_profile, profiles["default"])
        cfg.llm_profiles[cfg.default_profile] = default.override(
            base_url=env_base_url, model=env_model
        )
    return cfg
