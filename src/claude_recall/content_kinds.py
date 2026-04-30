"""Message content-kind classification (v0.8, issue #27).

Messages aren't one substance. Treating them as undifferentiated text was
the load-bearing design mistake that produced the issue #27 ranking failure
— the highest-mass content KINDS (system-injected harness blocks, agent
procedural narration, embedded tool-result code) dominated the top of
``topics`` over genuine topical content.

This module classifies each message into one of four kinds at index time
so downstream queries (``topics``, ``search``) can scope to the kind that
carries signal for them. The architectural pattern is borrowed from ANI
Runtime's Apr 10 2026 epistemic-grounding reform: walls between content
kinds enable better retrieval, not worse — the constraint is what
prevents contamination from amplifying through downstream stages.

The four kinds:

- ``THOUGHT``    — User prompts, agent substantive reasoning, design
                   discussions, analysis. Topical content lives here.
                   This is the slice ``topics`` operates on.
- ``PROCEDURAL`` — Agent narration ("Let me check…", "Now I'll…"),
                   tool-call announcements, build-cycle status. Useful
                   for "show me the moment I decided X" but not for
                   "what topics recur." Excluded from ``topics``.
- ``HARNESS``    — System-injected wrappers (``<ide_opened_file>``,
                   ``<task-notification>``, ``<system-reminder>``,
                   ``<user-summary>``) plus the canonical
                   auto-summarization opener. Pure infrastructure;
                   excluded from ``topics``.
- ``TOOL_RESULT_EMBEDDED`` — Assistant messages dominated by code or
                   data tokens, typically from quoted tool-result output
                   leaking inline. Excluded from ``topics``; opt-in via
                   ``search --include-tool-results``.

Default routing: ``topics`` queries ``THOUGHT``-only by default;
``search`` queries all kinds by default with a ``--kind`` flag.
"""

from __future__ import annotations

import re

THOUGHT = "THOUGHT"
PROCEDURAL = "PROCEDURAL"
HARNESS = "HARNESS"
TOOL_RESULT_EMBEDDED = "TOOL_RESULT_EMBEDDED"

VALID_KINDS = frozenset({THOUGHT, PROCEDURAL, HARNESS, TOOL_RESULT_EMBEDDED})

# HARNESS — wrapper-tag patterns. Match anchored at the start (after
# whitespace strip) so a message that merely mentions a wrapper tag
# inline is not misclassified.
_HARNESS_WRAPPER_TAGS = (
    "<ide_opened_file>",
    "<ide_diagnostic>",
    "<ide_selection>",
    "<task-notification>",
    "<system-reminder>",
    "<user-summary>",
    "<command-name>",
    "<command-message>",
    "<command-args>",
    "<local-command-stdout>",
    "<local-command-stderr>",
    # `<analysis>` blocks open the post-compact summary template Claude
    # Code emits for assistant messages summarizing prior context. The
    # tag itself isn't user/agent thought; it's a structured summary
    # output by the harness's compaction flow.
    "<analysis>",
)

# HARNESS — canonical openers. Anthropic's auto-compact triggers and
# session-continuation summaries have no enclosing tag; we identify them
# by the verbatim openers. This is brittle to wording changes by Anthropic
# — that brittleness is a feature: a test failure here is the right signal
# that the harness format changed and we should re-classify the corpus.
_HARNESS_CANONICAL_OPENERS = (
    "Your task is to create a detailed summary of the conversation so far",
    "This session is being continued from a previous conversation that ran out of context",
)

# HARNESS — system-role single-line markers (e.g., "Conversation compacted").
# When the role is explicitly `system`, virtually any short structured marker
# is harness content rather than conversation. We gate on length to avoid
# misclassifying any future system-role substantive content.
_HARNESS_SYSTEM_MAX_LEN = 200

# PROCEDURAL — narrator openers. Agent messages that start with these
# are announcing what the agent is about to do, not contributing
# topical content. The list intentionally covers the "let me / now I'll"
# register without trying to enumerate every conversational mechanic.
_PROCEDURAL_OPENERS = (
    "let me ",
    "let's ",
    "now let me ",
    "now i'll ",
    "now i will ",
    "now commit",
    "now push",
    "now run",
    "now build",
    "now check",
    "now test",
    "now verify",
    "now deploy",
    "i'll ",
    "i will check",
    "i will verify",
    "i will run",
    "i'm going to ",
    "going to ",
    "first, let me ",
    "first let me ",
    "next, let me ",
    "next let me ",
    "okay, let me ",
    "okay let me ",
    "ok, let me ",
    "ok let me ",
    "perfect! now ",
    "perfect! let me ",
    "perfect, now ",
    "perfect, let me ",
    "great! now ",
    "great! let me ",
    "great, now ",
    "great, let me ",
    "got it! ",
    "got it. ",
)

# PROCEDURAL — short title/announcement pattern: assistant messages that
# are essentially "Now <Subject>:" file/topic markers (e.g., "Now
# CrewService:", "Now R3:"). Length-gated so legitimate one-line agent
# observations don't get demoted.
_PROCEDURAL_NOW_TITLE = re.compile(r"^now\s+\S+:?\s*$", re.IGNORECASE)

# PROCEDURAL — short-status terms. Used in combination with a length
# threshold below: an agent message under 200 chars that contains one
# of these terms is classified PROCEDURAL.
_PROCEDURAL_STATUS_TERMS = (
    "check",
    "verify",
    "test",
    "run",
    "build",
    "compile",
    "fix",
    "update",
    "rebuild",
    "rerun",
    "passed",
    "failing",
    "compiles",
    "passes",
    "all green",
)

# Programming-language keywords used by the TOOL_RESULT_EMBEDDED
# heuristic. Not exhaustive — just the high-frequency tokens that
# distinguish a quoted code block from natural language.
_CODE_KEYWORDS = frozenset({
    "public", "private", "protected", "internal", "static", "void",
    "class", "interface", "struct", "enum", "namespace", "using",
    "import", "from", "return", "const", "let", "var", "func",
    "function", "def", "lambda", "async", "await", "throw", "throws",
    "try", "catch", "finally", "if", "else", "elif", "for", "while",
    "switch", "case", "break", "continue", "true", "false", "null",
    "none", "self", "this", "super", "extends", "implements", "new",
    "delete", "typeof", "instanceof", "yield",
})

_TOKEN_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# Code-shape tokens: camelCase, PascalCase, snake_case-with-underscore.
# A natural-English word is lowercase or Capitalized-First-Letter only;
# anything with an interior uppercase or interior underscore is code-shape.
_CAMEL_OR_SNAKE = re.compile(r"[a-z][A-Z]|[A-Z][a-z].*[A-Z]|_")
_CODE_PUNCT = set("{};()=<>[]")

PROCEDURAL_LENGTH_LIMIT = 200
TOOL_RESULT_CODE_DENSITY_THRESHOLD = 0.30
TOOL_RESULT_PUNCT_DENSITY_THRESHOLD = 0.04


def classify(content: str, role: str | None = None) -> str:
    """Classify a single message into one of the four content kinds.

    ``role`` is used to gate the PROCEDURAL and TOOL_RESULT_EMBEDDED
    classifiers (only assistant messages are eligible). User messages
    that aren't HARNESS go straight to THOUGHT — the user's prompts
    are the ground truth of what the conversation was about.
    """
    if not content:
        return THOUGHT

    role_normalized = (role or "").lower()

    if _is_harness(content, role_normalized):
        return HARNESS

    is_assistant = role_normalized == "assistant"

    if is_assistant and _is_tool_result_embedded(content):
        return TOOL_RESULT_EMBEDDED

    if is_assistant and _is_procedural(content):
        return PROCEDURAL

    return THOUGHT


def _is_harness(content: str, role: str = "") -> bool:
    stripped = content.lstrip()
    if any(stripped.startswith(opener) for opener in _HARNESS_CANONICAL_OPENERS):
        return True
    if any(stripped.startswith(tag) for tag in _HARNESS_WRAPPER_TAGS):
        return True
    # System-role short markers ("Conversation compacted", etc.) are
    # operational signals from the harness, not conversation.
    if role == "system" and len(stripped) <= _HARNESS_SYSTEM_MAX_LEN:
        return True
    return False


def _is_procedural(content: str) -> bool:
    stripped = content.strip()
    if not stripped:
        return False
    lower = stripped.lower()
    if any(lower.startswith(opener) for opener in _PROCEDURAL_OPENERS):
        return True
    if _PROCEDURAL_NOW_TITLE.match(stripped):
        return True
    # Short-status path: agent messages under PROCEDURAL_LENGTH_LIMIT chars
    # that use one of the status verbs are running commentary, not topical
    # content. Length gate keeps "I'll write a long technical analysis of
    # how to verify the migration works" in THOUGHT.
    if len(stripped) <= PROCEDURAL_LENGTH_LIMIT:
        if any(term in lower for term in _PROCEDURAL_STATUS_TERMS):
            return True
    return False


def _is_tool_result_embedded(content: str) -> bool:
    """Detect assistant messages dominated by quoted code/data.

    Two complementary signals — passing either is sufficient:

    - **Code-keyword density**: tokens drawn from a small programming-
      keyword set ("public", "class", "import", "return", etc.) make up
      >= 30% of all alphanumeric tokens. Hits when an assistant message
      is mostly a quoted source-file dump.
    - **Punctuation density**: code-punctuation characters (``{};()=<>[]``)
      make up >= 4% of the content. Hits when a message is dominated by
      structured output (config dumps, JSON, code) rather than prose.

    The thresholds are deliberately loose — false positives demote a
    legitimate THOUGHT message to TOOL_RESULT_EMBEDDED and exclude it
    from ``topics``, which is the right failure direction for a
    candidate-generation tool. False negatives leave noise in ``topics``,
    which is the failure direction issue #27 was filed to fix.
    """
    if len(content) < 80:
        # Short snippets (one-line acknowledgements with a code reference)
        # don't have enough signal for the density heuristics. Defer to
        # other classifiers.
        return False

    tokens = _TOKEN_PATTERN.findall(content)
    if tokens:
        keyword_hits = sum(1 for t in tokens if t.lower() in _CODE_KEYWORDS)
        camel_or_snake = sum(1 for t in tokens if _CAMEL_OR_SNAKE.search(t))
        density = (keyword_hits + camel_or_snake) / len(tokens)
        if density >= TOOL_RESULT_CODE_DENSITY_THRESHOLD:
            return True

    punct_count = sum(1 for ch in content if ch in _CODE_PUNCT)
    if punct_count / len(content) >= TOOL_RESULT_PUNCT_DENSITY_THRESHOLD:
        return True

    return False
