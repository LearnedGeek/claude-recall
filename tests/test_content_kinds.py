"""Tests for the content-kind classifier (v0.8, issue #27).

Each kind has a positive case (clearly that kind) and at least one
boundary case (almost that kind, but should fall through to a different
classification). The PROCEDURAL/THOUGHT boundary is the fuzziest and
gets the most coverage — it's the classifier most likely to need
tuning against real-world data.
"""

from claude_recall import content_kinds


# ---------------------------------------------------------------------------
# HARNESS — wrapper-tag patterns
# ---------------------------------------------------------------------------

def test_ide_opened_file_is_harness():
    content = (
        "<ide_opened_file>The user opened the file e:\\foo.md in the IDE. "
        "This may or may not be related to the current task.</ide_opened_file>"
    )
    assert content_kinds.classify(content, role="user") == content_kinds.HARNESS


def test_task_notification_is_harness():
    content = (
        "<task-notification>\n<task-id>abc</task-id>\n"
        "<output-file>C:\\tmp\\out</output-file>\n</task-notification>"
    )
    assert content_kinds.classify(content, role="user") == content_kinds.HARNESS


def test_system_reminder_is_harness():
    content = "<system-reminder>The TodoWrite tool hasn't been used recently.</system-reminder>"
    assert content_kinds.classify(content, role="user") == content_kinds.HARNESS


def test_user_summary_block_is_harness():
    content = "<user-summary>Recent activity: ...</user-summary>"
    assert content_kinds.classify(content, role="user") == content_kinds.HARNESS


def test_canonical_auto_summary_opener_is_harness():
    """The auto-compaction summary trigger has no enclosing tag — it's
    identified by its verbatim opener. Brittle to wording changes by
    Anthropic on purpose: a test failure is the right signal that the
    harness format changed and we need to re-classify the corpus."""
    content = (
        "Your task is to create a detailed summary of the conversation so far, "
        "paying attention to the user's explicit requests and your previous actions."
    )
    assert content_kinds.classify(content, role="user") == content_kinds.HARNESS


def test_session_continuation_opener_is_harness():
    """The 'session being continued' opener (post-compact) is the second
    canonical-string HARNESS pattern — surfaces in real archives at
    cluster-rank-2 size before classification."""
    content = (
        "This session is being continued from a previous conversation that ran out "
        "of context. The summary below covers the earlier portion of the conversation."
    )
    assert content_kinds.classify(content, role="user") == content_kinds.HARNESS


def test_system_role_short_marker_is_harness():
    """System-role short markers ('Conversation compacted', etc.) are
    operational signals from the harness, not conversation."""
    assert content_kinds.classify(
        "Conversation compacted", role="system"
    ) == content_kinds.HARNESS


def test_system_role_long_substantive_is_thought():
    """Long system-role content (>200 chars) does not get the short-marker
    HARNESS treatment. Future system-role substantive content (e.g., a
    real system message documenting state) stays THOUGHT."""
    content = (
        "System notice: the database connection pool is being reset due to "
        "a configuration change. Reconnects will retry with backoff. Affected "
        "tables include sessions, messages, message_vectors. Expect transient "
        "failures for the next 60 seconds while the pool stabilizes."
    )
    assert content_kinds.classify(content, role="system") == content_kinds.THOUGHT


def test_now_commit_and_push_is_procedural():
    """Short narrator pattern 'Now commit and push:' is one of the most
    common PROCEDURAL templates in the dev archive."""
    assert content_kinds.classify(
        "Now commit and push:", role="assistant"
    ) == content_kinds.PROCEDURAL


def test_now_title_announcement_is_procedural():
    """Short 'Now <Subject>:' file/topic announcements ('Now CrewService:',
    'Now R3:') are PROCEDURAL — assistant scaffolding the agent uses to
    introduce the next file or section it's reviewing."""
    for content in ("Now CrewService:", "Now R3:", "Now Spanish:"):
        assert (
            content_kinds.classify(content, role="assistant")
            == content_kinds.PROCEDURAL
        ), f"{content!r} should be PROCEDURAL"


def test_perfect_now_opener_is_procedural():
    """'Perfect! Now I have all the information I need...' is the most
    common compaction-completion narrator template in the assistant
    register. It's a transition signal, not topical content."""
    content = "Perfect! Now I have all the information I need to provide a comprehensive summary."
    assert content_kinds.classify(
        content, role="assistant"
    ) == content_kinds.PROCEDURAL


def test_analysis_block_opener_is_harness():
    """Post-compact summary blocks open with `<analysis>` and are
    structurally harness output, not user/agent thought."""
    content = (
        "<analysis>\nLet me trace through the conversation chronologically:\n"
        "1. Session start: continuation from prior session.</analysis>"
    )
    assert content_kinds.classify(content, role="assistant") == content_kinds.HARNESS


def test_message_mentioning_wrapper_tag_inline_is_not_harness():
    """A user message that *talks about* a wrapper tag without being one
    must not be misclassified. We anchor on the start of the trimmed
    content."""
    content = (
        "I'm wondering how to handle the <ide_opened_file> wrapper "
        "in our parser — should we always strip it?"
    )
    assert content_kinds.classify(content, role="user") == content_kinds.THOUGHT


# ---------------------------------------------------------------------------
# TOOL_RESULT_EMBEDDED — assistant messages dominated by code/data
# ---------------------------------------------------------------------------

def test_quoted_csharp_class_dump_is_tool_result_embedded():
    content = (
        "Here's the file:\n"
        "public class ApiCombatGame {\n"
        "    private readonly string _name;\n"
        "    public string Name => _name;\n"
        "    public ApiCombatGame(string name) {\n"
        "        _name = name;\n"
        "    }\n"
        "    public void Play() { Console.WriteLine($\"Playing {_name}\"); }\n"
        "}"
    )
    assert (
        content_kinds.classify(content, role="assistant")
        == content_kinds.TOOL_RESULT_EMBEDDED
    )


def test_short_code_reference_in_assistant_message_stays_thought():
    """A one-line code reference in a longer prose message is THOUGHT,
    not TOOL_RESULT_EMBEDDED. The density check requires both length
    and high code density."""
    content = "I'd recommend using `os.path.join` here for portability."
    assert content_kinds.classify(content, role="assistant") == content_kinds.THOUGHT


def test_user_role_quoting_code_is_thought():
    """User-role messages are never classified TOOL_RESULT_EMBEDDED.
    The user is the ground truth of what the conversation was about,
    even when they paste a code snippet to ask a question."""
    content = (
        "public class Foo { public int Bar() { return 42; } } "
        "Why does this throw NullReferenceException at runtime when called?"
    )
    # User-role: stays THOUGHT despite high code density.
    assert content_kinds.classify(content, role="user") == content_kinds.THOUGHT


# ---------------------------------------------------------------------------
# PROCEDURAL — agent narration
# ---------------------------------------------------------------------------

def test_let_me_check_opener_is_procedural():
    content = "Let me check the existing patterns in the codebase."
    assert (
        content_kinds.classify(content, role="assistant")
        == content_kinds.PROCEDURAL
    )


def test_now_i_will_opener_is_procedural():
    content = "Now I'll run the tests to verify the change works."
    assert (
        content_kinds.classify(content, role="assistant")
        == content_kinds.PROCEDURAL
    )


def test_short_status_with_verify_term_is_procedural():
    content = "All tests pass. Let me verify the build is clean."
    assert (
        content_kinds.classify(content, role="assistant")
        == content_kinds.PROCEDURAL
    )


def test_long_thoughtful_response_with_verify_word_stays_thought():
    """A long substantive analysis that happens to contain the word
    'verify' is THOUGHT, not PROCEDURAL — the length gate blocks the
    short-status path."""
    content = (
        "The architectural decision to verify each migration step "
        "individually has a real cost: every additional verification "
        "point is a potential drift source between intended and actual "
        "schema state. But the cost of *not* verifying — silent corruption "
        "across multiple migrations chained together, which is what we hit "
        "in the v0.4 cluster — is worse. The right balance is verify-once "
        "at migration boundaries, not within each step. This matches the "
        "pattern we landed on in ANI Runtime's epistemic-grounding refactor "
        "and what the v0.6 content-hash diff already does in our own indexer."
    )
    assert (
        content_kinds.classify(content, role="assistant")
        == content_kinds.THOUGHT
    )


def test_user_role_is_never_procedural():
    """Even a user message starting with 'Let me check' is THOUGHT.
    The PROCEDURAL classifier gates on assistant role only — user
    requests are always ground-truth content."""
    content = "Let me check what you're proposing — does it handle the v1 path?"
    assert content_kinds.classify(content, role="user") == content_kinds.THOUGHT


# ---------------------------------------------------------------------------
# THOUGHT — substantive content
# ---------------------------------------------------------------------------

def test_substantive_design_discussion_is_thought():
    content = (
        "I think we should split the migration into two commits — one for "
        "the schema change and one for the backfill — so a partial deploy "
        "doesn't leave the column unpopulated. The downside is two PRs to "
        "coordinate, but the upside is rollback safety."
    )
    assert content_kinds.classify(content, role="user") == content_kinds.THOUGHT


def test_empty_string_is_thought_default():
    """Empty content can't be classified usefully; default to THOUGHT
    so it's at least not silently excluded from queries."""
    assert content_kinds.classify("", role="user") == content_kinds.THOUGHT


def test_role_none_is_treated_as_user_role():
    """Missing role argument: PROCEDURAL/TOOL_RESULT gates close so
    the message defaults to THOUGHT (or HARNESS if it matches a
    wrapper tag)."""
    content = "Let me check that."
    assert content_kinds.classify(content, role=None) == content_kinds.THOUGHT
