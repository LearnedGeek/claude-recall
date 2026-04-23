"""Natural-language → FTS5 keyword extraction (v0.2).

Design note: PLAN §17 Decision 1 named spacy as the preferred backend.
Measurement during v0.2 scoping showed spacy's cold-import (~1-2s per
process) blows the 500ms UserPromptSubmit budget from §7.3, since each
hook invocation is a fresh Python process with no warm model cache.
The PLAN's own exception clause (*"unless a concrete reason emerges
during v0.2 scoping"*) authorizes the alternative.

Result: v0.2 ships a stdlib-only extractor that is a large step up from
the v0.1.1 fallback (which OR-joined every token including stopwords).
A future v0.3 can layer a warm-daemon spacy path behind a `[nlp]` extra
without changing this module's public surface.
"""

from __future__ import annotations

import re

# Conservative English stopword list. Not linguistically exhaustive — tuned for
# the natural-language prompt shapes that arrive in UserPromptSubmit hooks
# ("remind me", "can you show", "what did we decide", etc.). Over-pruning here
# is safer than under-pruning because BM25 ranking still picks the signal.
STOPWORDS: frozenset[str] = frozenset(
    {
        # pronouns + possessives
        "i", "me", "my", "mine", "myself",
        "you", "your", "yours", "yourself", "yourselves",
        "he", "him", "his", "himself",
        "she", "her", "hers", "herself",
        "it", "its", "itself",
        "we", "us", "our", "ours", "ourselves",
        "they", "them", "their", "theirs", "themselves",
        # determiners
        "a", "an", "the", "this", "that", "these", "those",
        "some", "any", "all", "each", "every", "no", "none",
        "other", "another", "several", "many", "much", "few",
        # aux + modal verbs
        "am", "is", "are", "was", "were", "be", "been", "being",
        "have", "has", "had", "having",
        "do", "does", "did", "doing",
        "will", "would", "shall", "should", "may", "might", "must", "can", "could",
        # prepositions
        "of", "for", "to", "from", "with", "without", "in", "on", "at", "by",
        "as", "about", "against", "between", "into", "through", "during",
        "before", "after", "above", "below", "up", "down", "over", "under",
        "within", "among",
        # conjunctions + common connectors
        "and", "or", "but", "if", "then", "else", "so", "because", "while",
        "although", "though", "since", "unless", "until",
        # wh-words + common interrogatives
        "what", "which", "who", "whom", "whose", "where", "when", "why", "how",
        # common natural-language filler in prompts
        "remind", "tell", "show", "explain", "describe", "give", "help",
        "please", "thanks", "thank",
        "like", "want", "need", "think", "know", "ok", "okay", "sure",
        "also", "just", "only", "very", "really", "maybe", "probably",
        "here", "there", "now", "today", "yesterday", "tomorrow",
        "again", "still", "already", "yet", "anymore",
        # adverbs that rarely carry topical signal
        "not", "nor", "too", "than",
        # single letters (stray from punctuation splits)
        "s", "t", "m", "d", "ll", "re", "ve",
    }
)

_QUOTED = re.compile(r'"([^"]+)"')
# Tokens: letter-leading, allow digits/underscore/hyphen inside. Min 2 chars.
_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_\-]+")


def extract_keywords(prompt: str, min_len: int = 3) -> list[str]:
    """Return a deduplicated, order-preserving list of keywords from a prompt.

    - Preserves double-quoted phrases as single keywords (case preserved).
    - Strips stopwords from individual tokens.
    - Lowercases single tokens and deduplicates (case-insensitive).
    - Drops tokens shorter than ``min_len``.

    The order of the returned list reflects the order terms appear in the
    prompt. Later passes may reorder by TF/POS/etc.; MVP keeps it stable.
    """
    if not prompt:
        return []

    quoted_phrases = [q.strip() for q in _QUOTED.findall(prompt) if q.strip()]
    remainder = _QUOTED.sub(" ", prompt)

    seen: set[str] = set()
    out: list[str] = []

    for phrase in quoted_phrases:
        key = phrase.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(phrase)

    for token in _TOKEN.findall(remainder):
        lower = token.lower()
        if len(lower) < min_len:
            continue
        if lower in STOPWORDS:
            continue
        if lower in seen:
            continue
        seen.add(lower)
        out.append(lower)

    return out


def build_fts_query(keywords: list[str]) -> str:
    """OR-join a keyword list into an FTS5 query string.

    Multi-word keywords (quoted phrases from the original prompt) are
    re-wrapped in double quotes so FTS5 treats them as phrase matches.
    Single tokens are emitted bare so Porter stemming applies.

    Empty input returns an empty string — callers must guard before passing
    to FTS5 MATCH.
    """
    if not keywords:
        return ""
    parts: list[str] = []
    for kw in keywords:
        if " " in kw:
            # Escape embedded quotes per FTS5 string syntax (double the quote).
            escaped = kw.replace('"', '""')
            parts.append(f'"{escaped}"')
        else:
            parts.append(kw)
    return " OR ".join(parts)
