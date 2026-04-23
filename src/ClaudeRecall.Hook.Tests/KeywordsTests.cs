// Parallel of tests/test_keywords.py. If the Python tests change, mirror here.

using Xunit;

namespace ClaudeRecall.Hook.Tests;

public class KeywordsTests
{
    [Fact]
    public void Extract_EmptyPromptReturnsEmpty()
    {
        Assert.Empty(Keywords.Extract(""));
    }

    [Fact]
    public void Extract_WhitespacePromptReturnsEmpty()
    {
        Assert.Empty(Keywords.Extract("   \n\t "));
    }

    [Fact]
    public void Extract_StripsStopwords()
    {
        var result = Keywords.Extract("what did we decide about regex patterns");
        var lower = result.Select(k => k.ToLowerInvariant()).ToHashSet();
        foreach (var s in new[] { "what", "did", "we", "about" })
        {
            Assert.DoesNotContain(s, lower);
        }
        Assert.Contains("decide", lower);
        Assert.Contains("regex", lower);
        Assert.Contains("patterns", lower);
    }

    [Fact]
    public void Extract_DogfoodingPromptKeepsTopicalTokens()
    {
        var result = Keywords.Extract("remind me what we decided about regex patterns");
        var lower = result.Select(k => k.ToLowerInvariant()).ToHashSet();
        Assert.Superset(new HashSet<string> { "decided", "regex", "patterns" }, lower);
        foreach (var f in new[] { "remind", "what", "we", "about", "me" })
        {
            Assert.DoesNotContain(f, result);
        }
    }

    [Fact]
    public void Extract_QuotedPhrasePreserved()
    {
        var result = Keywords.Extract("show me \"inner thought prompt builder\" notes");
        Assert.Contains("inner thought prompt builder", result);
        // Tokens inside the quoted phrase should NOT re-appear bare.
        Assert.DoesNotContain("inner", result);
        Assert.DoesNotContain("thought", result);
    }

    [Fact]
    public void Extract_DeduplicatesCaseInsensitive()
    {
        var result = Keywords.Extract("Regex and regex and REGEX");
        var lower = result.Select(k => k.ToLowerInvariant()).ToList();
        Assert.Single(lower.Where(k => k == "regex"));
    }

    [Fact]
    public void Extract_MinLengthFilter()
    {
        var result = Keywords.Extract("go to fly a kite", minLength: 3);
        // "go", "to", "a" — either stopword or too short
        Assert.DoesNotContain("go", result);
        Assert.True(result.Contains("fly") || result.Contains("kite"));
    }

    [Fact]
    public void BuildQuery_BareTokens()
    {
        Assert.Equal(
            "regex OR patterns OR decisions",
            Keywords.BuildFtsQuery(["regex", "patterns", "decisions"]));
    }

    [Fact]
    public void BuildQuery_QuotesMultiWordPhrases()
    {
        var q = Keywords.BuildFtsQuery(["inner thought prompt builder", "regex"]);
        Assert.StartsWith("\"inner thought prompt builder\"", q);
        Assert.Contains("OR regex", q);
    }

    [Fact]
    public void BuildQuery_EmptyReturnsEmpty()
    {
        Assert.Equal(string.Empty, Keywords.BuildFtsQuery([]));
    }

    [Fact]
    public void BuildQuery_EscapesEmbeddedQuotes()
    {
        var q = Keywords.BuildFtsQuery(["he said \"hi\" then"]);
        Assert.Contains("\"\"hi\"\"", q);
    }

    [Fact]
    public void Extract_OrderPreserved()
    {
        var result = Keywords.Extract("regex appears before patterns here");
        Assert.True(result.IndexOf("regex") < result.IndexOf("patterns"));
    }

    [Fact]
    public void Extract_AllowsIdentifierShapedTokens()
    {
        var result = Keywords.Extract("check the v0_2_1 build and test_runner output");
        Assert.Contains("v0_2_1", result);
        Assert.Contains("test_runner", result);
    }
}
