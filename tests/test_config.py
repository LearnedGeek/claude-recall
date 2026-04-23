"""Tests for claude_recall.config.

See docs/PLAN.md sections 8 and 11.
"""

from pathlib import Path

from claude_recall import config


def test_defaults_when_file_missing(tmp_path):
    """Missing config file yields a pure-defaults Config."""
    cfg = config.load_config(tmp_path / "nonexistent.toml")
    default = config.Config()
    assert cfg.archive_root == default.archive_root
    assert cfg.db_path == default.db_path
    assert cfg.search.hook_threshold == default.search.hook_threshold
    assert cfg.indexing.index_tool_blocks is False
    assert cfg.embeddings.enabled is False


def test_toml_overrides_defaults(tmp_path):
    """Values set in the TOML file override the dataclass defaults."""
    path = tmp_path / "config.toml"
    path.write_text(
        """
[archive]
root = "/tmp/fake-archive"

[database]
path = "/tmp/fake.db"

[search]
hook_threshold = 0.75
hook_limit = 5
max_injected_tokens = 1500
hook_days = 14

[indexing]
index_tool_blocks = true

[embeddings]
enabled = true
ollama_base_url = "http://localhost:9999"
model = "other-model"
""",
        encoding="utf-8",
    )
    cfg = config.load_config(path)
    assert cfg.archive_root == Path("/tmp/fake-archive")
    assert cfg.db_path == Path("/tmp/fake.db")
    assert cfg.search.hook_threshold == 0.75
    assert cfg.search.hook_limit == 5
    assert cfg.search.max_injected_tokens == 1500
    assert cfg.search.hook_days == 14
    assert cfg.indexing.index_tool_blocks is True
    assert cfg.embeddings.enabled is True
    assert cfg.embeddings.ollama_base_url == "http://localhost:9999"
    assert cfg.embeddings.model == "other-model"


def test_partial_toml_keeps_other_defaults(tmp_path):
    """A partial config overrides only the specified fields."""
    path = tmp_path / "config.toml"
    path.write_text(
        """
[search]
hook_threshold = 0.9
""",
        encoding="utf-8",
    )
    cfg = config.load_config(path)
    default = config.Config()
    assert cfg.search.hook_threshold == 0.9
    assert cfg.search.hook_limit == default.search.hook_limit
    assert cfg.archive_root == default.archive_root


def test_home_expansion(tmp_path):
    """~ is expanded in archive.root and database.path."""
    path = tmp_path / "config.toml"
    path.write_text(
        """
[archive]
root = "~/my-archive"

[database]
path = "~/my-db/index.db"
""",
        encoding="utf-8",
    )
    cfg = config.load_config(path)
    assert cfg.archive_root == Path.home() / "my-archive"
    assert cfg.db_path == Path.home() / "my-db" / "index.db"


def test_default_paths_platform_correct(monkeypatch):
    """default_config_path / default_db_path honor XDG/APPDATA conventions."""
    import sys as _sys

    if _sys.platform == "win32":
        monkeypatch.setenv("APPDATA", "C:\\fake-appdata")
        assert "fake-appdata" in str(config.default_config_path())
        assert "fake-appdata" in str(config.default_db_path())
    else:
        monkeypatch.setenv("XDG_CONFIG_HOME", "/tmp/fake-xdg")
        assert str(config.default_config_path()).startswith("/tmp/fake-xdg")
        assert str(config.default_db_path()).startswith("/tmp/fake-xdg")
