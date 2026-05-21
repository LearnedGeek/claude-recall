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
    rerank_pool_size: int = 50
    request_timeout_seconds: float = 10.0
    batch_size: int = 32
    # Per-input character truncation cap. nomic-embed-text's context is
    # ~2048 tokens, which is roughly 6000-8000 English chars. A single
    # oversized input used to fail the entire 32-message batch (issue #8);
    # truncating up-front avoids that amplification. 6000 chars is a
    # conservative default (~1500 tokens) with headroom for the model's
    # internal overhead.
    max_input_chars: int = 6000
    # Ollama keep_alive directive sent on every embed call. Default 30m
    # keeps the embed model warm across a coding session, eliminating the
    # ~4s cold-load cost each time nomic-embed-text would otherwise unload
    # (Ollama default keep_alive=5m, which is much shorter than typical
    # between-prompt gaps). Set to "0" to unload after every call, or "-1"
    # to keep loaded indefinitely. Issue #11.
    keep_alive: str = "30m"
    # Hook uses --semantic when true AND enabled is true.
    # v0.4: default flipped to true — the C# NativeAOT hook binary brings
    # semantic-on hook latency to ~30-80ms (well under PLAN §7.3's 500ms
    # budget). If you're still on the Python hook path (no binary in wheel,
    # or pre-v0.4 install), flip this back to false to stay under budget.
    use_in_hook: bool = True


@dataclass
class HooksConfig:
    # Issue #26 (v0.6.8): when true, init-hooks emits a sibling
    # UserPromptSubmit hook that injects the current local time as
    # additionalContext. Solves Claude's chronic temporal-drift problem
    # (suggesting "go to sleep" at 2:30pm because the model has no
    # ground-truth time). Default false — opt-in only. Marker-identified
    # so init-hooks --force can preserve, update, or remove cleanly
    # without touching any user-managed sibling hooks.
    inject_time: bool = False

    # Issue #29 (v0.9.0): when true, the managed UserPromptSubmit hook
    # reads `interventions_path` (project-relative by default) and
    # prepends its content to the additionalContext alongside the recall
    # search result. Architectural pattern: structural channel injection
    # puts the content in the context window BEFORE pattern recognition
    # + recall happens, so the agent literally cannot draft without
    # seeing it. Third deployment of the cross-project tier-separation
    # pattern (ANI memory tiers → claude-recall content_kind classifier
    # → this). Default false — opt-in only, additive.
    inject_interventions: bool = False
    interventions_path: str = ".claude/hooks/interventions.md"


@dataclass
class Config:
    archive_root: Path = field(default_factory=lambda: Path.home() / ".claude" / "projects")
    db_path: Path = field(default_factory=lambda: default_db_path())
    search: SearchConfig = field(default_factory=SearchConfig)
    indexing: IndexingConfig = field(default_factory=IndexingConfig)
    embeddings: EmbeddingsConfig = field(default_factory=EmbeddingsConfig)
    hooks: HooksConfig = field(default_factory=HooksConfig)


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

    Missing file → defaults silently. Full TOML parsing + precedence is
    implemented in the dedicated config step.
    """
    if path is None:
        path = default_config_path()
    if not path.exists():
        return Config()
    return _load_toml(path)


def _load_toml(path: Path) -> Config:
    with open(path, "rb") as fh:
        data = tomllib.load(fh)

    cfg = Config()
    archive_root = _dig(data, "archive", "root")
    if isinstance(archive_root, str):
        cfg.archive_root = Path(archive_root).expanduser()

    db_path = _dig(data, "database", "path")
    if isinstance(db_path, str):
        cfg.db_path = Path(db_path).expanduser()

    for field_name in ("hook_threshold", "hook_limit", "max_injected_tokens", "hook_days"):
        val = _dig(data, "search", field_name)
        if val is not None:
            setattr(cfg.search, field_name, val)

    tool_blocks = _dig(data, "indexing", "index_tool_blocks")
    if tool_blocks is not None:
        cfg.indexing.index_tool_blocks = bool(tool_blocks)

    for field_name in (
        "enabled",
        "ollama_base_url",
        "model",
        "rerank_pool_size",
        "request_timeout_seconds",
        "batch_size",
        "max_input_chars",
        "keep_alive",
        "use_in_hook",
    ):
        val = _dig(data, "embeddings", field_name)
        if val is not None:
            setattr(cfg.embeddings, field_name, val)

    inject_time = _dig(data, "hooks", "inject_time")
    if inject_time is not None:
        cfg.hooks.inject_time = bool(inject_time)

    inject_interventions = _dig(data, "hooks", "inject_interventions")
    if inject_interventions is not None:
        cfg.hooks.inject_interventions = bool(inject_interventions)

    interventions_path = _dig(data, "hooks", "interventions_path")
    if isinstance(interventions_path, str):
        cfg.hooks.interventions_path = interventions_path

    return cfg


def _dig(data: dict, *path: str):
    node = data
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node
