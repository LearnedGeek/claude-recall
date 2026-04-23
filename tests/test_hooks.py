"""Tests for the shell hook scripts.

These tests subprocess-invoke the hook scripts with controlled stdin and verify
that stdout is valid JSON matching the hook contract from docs/PLAN.md section 7.

See docs/PLAN.md section 11 for the full test plan.
"""

import pytest


@pytest.mark.skip(reason="hook scripts not yet implemented end-to-end")
def test_session_start_hook_emits_valid_json(tmp_path):
    """SessionStart hook stdout parses as JSON with additionalContext field."""
    pass


@pytest.mark.skip(reason="hook scripts not yet implemented end-to-end")
def test_on_prompt_hook_emits_valid_json(tmp_path):
    """UserPromptSubmit hook stdout parses as JSON (either {} or with additionalContext)."""
    pass


@pytest.mark.skip(reason="hook scripts not yet implemented end-to-end")
def test_on_prompt_empty_on_error(tmp_path):
    """When CLI fails, hook still exits 0 and emits empty {}."""
    pass


@pytest.mark.skip(reason="hook scripts not yet implemented end-to-end")
def test_on_prompt_within_budget(archive_dir):
    """UserPromptSubmit hook completes in < 500ms on a sample indexed archive."""
    pass
