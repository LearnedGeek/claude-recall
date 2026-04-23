"""Configuration loading — TOML file + defaults + CLI override precedence.

Config location: ~/.config/claude-recall/config.toml on Unix,
%APPDATA%/claude-recall/config.toml on Windows.

See docs/PLAN.md section 8 for the full schema.
"""

import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class SearchConfig:
    hook_threshold: float = 0.3
    hook_limit: int = 3
    max_injected_tokens: int = 800
    hook_days: int = 30


@dataclass
class IndexingConfig:
    index_tool_blocks: bool = False


@dataclass
class EmbeddingsConfig:
    enabled: bool = False
    ollama_base_url: str = "http://localhost:11434"
    model: str = "nomic-embed-text"


@dataclass
class Config:
    archive_root: Path = field(default_factory=lambda: Path.home() / ".claude" / "projects")
    db_path: Path = field(default_factory=lambda: default_db_path())
    search: SearchConfig = field(default_factory=SearchConfig)
    indexing: IndexingConfig = field(default_factory=IndexingConfig)
    embeddings: EmbeddingsConfig = field(default_factory=EmbeddingsConfig)


def default_config_path() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home())) / "claude-recall" / "config.toml"
    # Unix: respect XDG_CONFIG_HOME if set
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "claude-recall" / "config.toml"


def default_db_path() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home())) / "claude-recall" / "index.db"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "claude-recall" / "index.db"


def load_config(path: Path | None = None) -> Config:
    """Load config from TOML file, falling back to defaults.

    TODO(implementer):
    - If path is None, use default_config_path()
    - If file doesn't exist, return Config() (all defaults)
    - Parse TOML via tomllib; apply to Config dataclass
    - Expand ~ in any path fields
    - Return Config instance
    """
    raise NotImplementedError("See docs/PLAN.md section 8 for schema.")
