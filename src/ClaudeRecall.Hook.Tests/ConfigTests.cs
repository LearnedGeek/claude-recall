using Xunit;

namespace ClaudeRecall.Hook.Tests;

public class ConfigTests : IDisposable
{
    private readonly string _tmpDir;

    public ConfigTests()
    {
        _tmpDir = Path.Combine(Path.GetTempPath(), $"crhook-cfg-{Guid.NewGuid():N}");
        Directory.CreateDirectory(_tmpDir);
    }

    public void Dispose()
    {
        if (Directory.Exists(_tmpDir))
        {
            Directory.Delete(_tmpDir, recursive: true);
        }
    }

    [Fact]
    public void Load_MissingFileReturnsDefaults()
    {
        var cfg = Config.Load(Path.Combine(_tmpDir, "does-not-exist.toml"));
        var def = new Config();
        Assert.Equal(def.HookThreshold, cfg.HookThreshold);
        Assert.Equal(def.EmbeddingsEnabled, cfg.EmbeddingsEnabled);
        Assert.Equal(def.UseInHook, cfg.UseInHook);
    }

    [Fact]
    public void Load_TomlOverridesDefaults()
    {
        var path = Path.Combine(_tmpDir, "config.toml");
        File.WriteAllText(path, """
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
            rerank_pool_size = 75
            request_timeout_seconds = 15.5
            batch_size = 16
            use_in_hook = true
            """);

        var cfg = Config.Load(path);
        Assert.Equal("/tmp/fake-archive", cfg.ArchiveRoot);
        Assert.Equal("/tmp/fake.db", cfg.DbPath);
        Assert.Equal(0.75, cfg.HookThreshold);
        Assert.Equal(5, cfg.HookLimit);
        Assert.Equal(1500, cfg.MaxInjectedTokens);
        Assert.Equal(14, cfg.HookDays);
        Assert.True(cfg.IndexToolBlocks);
        Assert.True(cfg.EmbeddingsEnabled);
        Assert.Equal("http://localhost:9999", cfg.OllamaBaseUrl);
        Assert.Equal("other-model", cfg.EmbeddingsModel);
        Assert.Equal(75, cfg.RerankPoolSize);
        Assert.Equal(15.5, cfg.RequestTimeoutSeconds);
        Assert.Equal(16, cfg.BatchSize);
        Assert.True(cfg.UseInHook);
    }

    [Fact]
    public void Load_PartialTomlKeepsDefaults()
    {
        var path = Path.Combine(_tmpDir, "config.toml");
        File.WriteAllText(path, "[search]\nhook_threshold = 0.9\n");

        var cfg = Config.Load(path);
        var def = new Config();
        Assert.Equal(0.9, cfg.HookThreshold);
        Assert.Equal(def.HookLimit, cfg.HookLimit);
        Assert.Equal(def.ArchiveRoot, cfg.ArchiveRoot);
    }

    [Fact]
    public void Load_MalformedTomlReturnsDefaults()
    {
        var path = Path.Combine(_tmpDir, "config.toml");
        File.WriteAllText(path, "this is not valid toml @@@[unclosed");

        var cfg = Config.Load(path);
        var def = new Config();
        Assert.Equal(def.HookThreshold, cfg.HookThreshold);
        Assert.Equal(def.EmbeddingsEnabled, cfg.EmbeddingsEnabled);
    }

    [Fact]
    public void Load_ExpandsTildeInPaths()
    {
        var path = Path.Combine(_tmpDir, "config.toml");
        File.WriteAllText(path, """
            [archive]
            root = "~/my-archive"

            [database]
            path = "~/my-db/index.db"
            """);

        var cfg = Config.Load(path);
        Assert.DoesNotContain("~", cfg.ArchiveRoot);
        Assert.DoesNotContain("~", cfg.DbPath);
        Assert.EndsWith("my-archive", cfg.ArchiveRoot);
        Assert.EndsWith("index.db", cfg.DbPath);
    }

    [Fact]
    public void DefaultConfigPath_IsPlatformAppropriate()
    {
        var path = Config.DefaultConfigPath();
        Assert.Contains("claude-recall", path);
        Assert.EndsWith("config.toml", path);
    }
}
