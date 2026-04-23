"""Tests for claude_recall.keywords (v0.2 keyword extraction).

See docs/PLAN.md §17 Decision 1 for the NLP-vs-stdlib tradeoff rationale.
"""

from claude_recall import keywords


def test_empty_prompt_returns_empty():
    assert keywords.extract_keywords("") == []


def test_whitespace_prompt_returns_empty():
    assert keywords.extract_keywords("   \n\t ") == []


def test_stopwords_stripped():
    """Common English stopwords do not survive extraction."""
    result = keywords.extract_keywords("what did we decide about regex patterns")
    lower = [k.lower() for k in result]
    for stop in ("what", "did", "we", "about"):
        assert stop not in lower
    # Content words survive
    assert "decide" in lower
    assert "regex" in lower
    assert "patterns" in lower


def test_natural_language_prompt_keeps_topical_tokens():
    """The dogfooding failure prompt extracts to the tokens the user cares about."""
    result = keywords.extract_keywords(
        "remind me what we decided about regex patterns"
    )
    # The filler words are out; the topical words remain.
    assert set(result) >= {"decided", "regex", "patterns"}
    for filler in ("remind", "what", "we", "about", "me"):
        assert filler not in result


def test_quoted_phrase_preserved_as_single_keyword():
    result = keywords.extract_keywords('show me "inner thought prompt builder" notes')
    assert "inner thought prompt builder" in result
    # Tokens from the quoted phrase are NOT re-extracted as individual words.
    assert "inner" not in result
    assert "thought" not in result


def test_deduplication_case_insensitive():
    result = keywords.extract_keywords("Regex and regex and REGEX")
    lower = [k.lower() for k in result]
    assert lower.count("regex") == 1


def test_min_length_filter():
    result = keywords.extract_keywords("go to fly a kite", min_len=3)
    # "go", "to", "a" — all filtered (stopword and/or too short).
    # "fly" and "kite" survive.
    assert "go" not in result
    assert "fly" in result or "kite" in result


def test_build_fts_query_bare_tokens():
    q = keywords.build_fts_query(["regex", "patterns", "decisions"])
    assert q == "regex OR patterns OR decisions"


def test_build_fts_query_phrases_quoted():
    q = keywords.build_fts_query(["inner thought prompt builder", "regex"])
    assert q.startswith('"inner thought prompt builder"')
    assert "OR regex" in q


def test_build_fts_query_empty_input():
    assert keywords.build_fts_query([]) == ""


def test_build_fts_query_escapes_embedded_quotes():
    q = keywords.build_fts_query(['he said "hi" then'])
    # FTS5 escapes double quotes by doubling them.
    assert '""hi""' in q


def test_order_preserved():
    result = keywords.extract_keywords("regex appears before patterns here")
    # "appears" is not a stopword; "here" is.
    assert result.index("regex") < result.index("patterns")


def test_alphanumeric_and_underscore_tokens():
    """Identifier-shaped tokens (snake_case, mixed, hyphenated) survive."""
    result = keywords.extract_keywords("check the v0_2_1 build and test_runner output")
    assert "v0_2_1" in result
    assert "test_runner" in result
