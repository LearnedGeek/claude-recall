// Keywords.cs — port of src/claude_recall/keywords.py.
//
// Strips stopwords/pronouns/fillers from a natural-language prompt and
// returns the topical keywords in order of appearance. Output is used to
// build an OR-joined FTS5 query string.
//
// Must produce the same output as the Python version on the same input so
// hook results match CLI results on identical prompts.

using System.Collections.Frozen;
using System.Text.RegularExpressions;

namespace ClaudeRecall.Hook;

internal static partial class Keywords
{
    // Stopword list — identical to keywords.py::STOPWORDS.
    public static readonly FrozenSet<string> Stopwords = FrozenSet.ToFrozenSet(new[]
    {
        // pronouns + possessives
        "i", "me", "my", "mine", "myself",
        "you", "your", "yours", "yourself", "yourselves",
        "he", "him", "his", "himself",
        "she", "her", "hers", "herself",
        "it", "its", "itself",
        "we", "us", "our", "ours", "ourselves",
        "they", "them", "their", "theirs", "themselves",
        // determiners
        "a", "an", "the", "this", "that", "these", "those",
        "some", "any", "all", "each", "every", "no", "none",
        "other", "another", "several", "many", "much", "few",
        // aux + modal verbs
        "am", "is", "are", "was", "were", "be", "been", "being",
        "have", "has", "had", "having",
        "do", "does", "did", "doing",
        "will", "would", "shall", "should", "may", "might", "must", "can", "could",
        // prepositions
        "of", "for", "to", "from", "with", "without", "in", "on", "at", "by",
        "as", "about", "against", "between", "into", "through", "during",
        "before", "after", "above", "below", "up", "down", "over", "under",
        "within", "among",
        // conjunctions + common connectors
        "and", "or", "but", "if", "then", "else", "so", "because", "while",
        "although", "though", "since", "unless", "until",
        // wh-words
        "what", "which", "who", "whom", "whose", "where", "when", "why", "how",
        // common natural-language filler in prompts
        "remind", "tell", "show", "explain", "describe", "give", "help",
        "please", "thanks", "thank",
        "like", "want", "need", "think", "know", "ok", "okay", "sure",
        "also", "just", "only", "very", "really", "maybe", "probably",
        "here", "there", "now", "today", "yesterday", "tomorrow",
        "again", "still", "already", "yet", "anymore",
        // adverbs that rarely carry topical signal
        "not", "nor", "too", "than",
        // single letters (stray from punctuation splits)
        "s", "t", "m", "d", "ll", "re", "ve",
    }, StringComparer.OrdinalIgnoreCase);

    [GeneratedRegex("\"([^\"]+)\"", RegexOptions.Compiled)]
    private static partial Regex QuotedRegex();

    [GeneratedRegex(@"[A-Za-z][A-Za-z0-9_\-]+", RegexOptions.Compiled)]
    private static partial Regex TokenRegex();

    /// <summary>
    /// Extract topical keywords from a prompt. Preserves quoted phrases as
    /// single keywords (case retained), lowercases bare tokens, deduplicates
    /// case-insensitively, drops tokens shorter than ``minLength`` and any
    /// stopword hit.
    /// </summary>
    public static List<string> Extract(string? prompt, int minLength = 3)
    {
        var result = new List<string>();
        if (string.IsNullOrWhiteSpace(prompt))
        {
            return result;
        }

        var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);

        // Quoted phrases first — they preserve original casing.
        foreach (Match m in QuotedRegex().Matches(prompt))
        {
            var phrase = m.Groups[1].Value.Trim();
            if (phrase.Length == 0 || !seen.Add(phrase))
            {
                continue;
            }
            result.Add(phrase);
        }

        // Remove quoted sections so their tokens aren't re-extracted bare.
        var remainder = QuotedRegex().Replace(prompt, " ");
        foreach (Match m in TokenRegex().Matches(remainder))
        {
            var token = m.Value;
            if (token.Length < minLength)
            {
                continue;
            }
            if (Stopwords.Contains(token))
            {
                continue;
            }
            var lower = token.ToLowerInvariant();
            if (!seen.Add(lower))
            {
                continue;
            }
            result.Add(lower);
        }

        return result;
    }

    /// <summary>
    /// OR-join keywords into an FTS5 query. Multi-word keywords (quoted
    /// phrases) become phrase matches; single tokens are bare so Porter
    /// stemming applies. Empty input returns empty string — callers guard
    /// before passing to FTS5 MATCH.
    /// </summary>
    public static string BuildFtsQuery(IReadOnlyList<string> keywords)
    {
        if (keywords.Count == 0)
        {
            return string.Empty;
        }
        var parts = new List<string>(keywords.Count);
        foreach (var kw in keywords)
        {
            if (kw.Contains(' '))
            {
                // FTS5 escapes double quotes by doubling them.
                parts.Add($"\"{kw.Replace("\"", "\"\"")}\"");
            }
            else
            {
                parts.Add(kw);
            }
        }
        return string.Join(" OR ", parts);
    }
}
