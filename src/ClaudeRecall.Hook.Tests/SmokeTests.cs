using Xunit;

namespace ClaudeRecall.Hook.Tests;

public class SmokeTests
{
    [Fact]
    public void CliArgs_DefaultsAreAllFalseOrNull()
    {
        var args = CliArgs.Parse(Array.Empty<string>());
        Assert.False(args.ShowVersion);
        Assert.False(args.Probe);
        Assert.False(args.NoSemantic);
        Assert.False(args.Verbose);
        Assert.Null(args.ConfigPath);
        Assert.Null(args.TimeoutMs);
    }

    [Fact]
    public void CliArgs_VersionFlagRecognized()
    {
        var args = CliArgs.Parse(["--version"]);
        Assert.True(args.ShowVersion);
    }

    [Fact]
    public void CliArgs_ProbeFlagRecognized()
    {
        var args = CliArgs.Parse(["--probe"]);
        Assert.True(args.Probe);
    }

    [Fact]
    public void CliArgs_ConfigPathConsumesNextArg()
    {
        var args = CliArgs.Parse(["--config", "C:/path/to/config.toml"]);
        Assert.Equal("C:/path/to/config.toml", args.ConfigPath);
    }

    [Fact]
    public void CliArgs_TimeoutMsParsesInt()
    {
        var args = CliArgs.Parse(["--timeout-ms", "500"]);
        Assert.Equal(500, args.TimeoutMs);
    }

    [Fact]
    public void CliArgs_UnknownFlagsIgnored()
    {
        var args = CliArgs.Parse(["--not-a-real-flag", "whatever"]);
        Assert.False(args.ShowVersion);
    }

    [Fact]
    public void Version_IsSemVer()
    {
        Assert.Matches(@"^\d+\.\d+\.\d+$", Program.Version);
    }
}
