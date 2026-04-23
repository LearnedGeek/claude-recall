// Config.cs — TOML config reader, mirrors src/claude_recall/config.py.
//
// Binary and Python CLI share config.toml. Defaults here must match the
// Python dataclasses byte-for-byte so behavior is identical.

using Tomlyn;
using Tomlyn.Model;

namespace ClaudeRecall.Hook;

internal sealed class Config
{
    // Defaults — keep in sync with src/claude_recall/config.py dataclasses.
    public string ArchiveRoot { get; init; } =
        Path.Combine(HomeDir(), ".claude", "projects");

    public string DbPath { get; init; } = DefaultDbPath();

    // [search]
    public double HookThreshold { get; init; } = 0.3;
    public int HookLimit { get; init; } = 3;
    public int MaxInjectedTokens { get; init; } = 800;
    public int HookDays { get; init; } = 30;

    // [indexing]
    public bool IndexToolBlocks { get; init; } = false;

    // [embeddings]
    public bool EmbeddingsEnabled { get; init; } = false;
    public string OllamaBaseUrl { get; init; } = "http://localhost:11434";
    public string EmbeddingsModel { get; init; } = "nomic-embed-text";
    public int RerankPoolSize { get; init; } = 50;
    public double RequestTimeoutSeconds { get; init; } = 10.0;
    public int BatchSize { get; init; } = 32;
    // v0.4 default true — see src/claude_recall/config.py for rationale.
    public bool UseInHook { get; init; } = true;

    /// <summary>Load config from ``path`` or defaults if the file is missing.</summary>
    public static Config Load(string? path = null)
    {
        path ??= DefaultConfigPath();
        if (string.IsNullOrEmpty(path) || !File.Exists(path))
        {
            return new Config();
        }

        TomlTable root;
        try
        {
            root = Toml.ToModel(File.ReadAllText(path));
        }
        catch
        {
            // Malformed TOML → silent defaults. Graceful degradation.
            return new Config();
        }

        var def = new Config();
        return new Config
        {
            ArchiveRoot = GetString(root, "archive", "root", def.ArchiveRoot, expandTilde: true),
            DbPath = GetString(root, "database", "path", def.DbPath, expandTilde: true),

            HookThreshold = GetDouble(root, "search", "hook_threshold", def.HookThreshold),
            HookLimit = GetInt(root, "search", "hook_limit", def.HookLimit),
            MaxInjectedTokens = GetInt(root, "search", "max_injected_tokens", def.MaxInjectedTokens),
            HookDays = GetInt(root, "search", "hook_days", def.HookDays),

            IndexToolBlocks = GetBool(root, "indexing", "index_tool_blocks", def.IndexToolBlocks),

            EmbeddingsEnabled = GetBool(root, "embeddings", "enabled", def.EmbeddingsEnabled),
            OllamaBaseUrl = GetString(root, "embeddings", "ollama_base_url", def.OllamaBaseUrl),
            EmbeddingsModel = GetString(root, "embeddings", "model", def.EmbeddingsModel),
            RerankPoolSize = GetInt(root, "embeddings", "rerank_pool_size", def.RerankPoolSize),
            RequestTimeoutSeconds = GetDouble(root, "embeddings", "request_timeout_seconds", def.RequestTimeoutSeconds),
            BatchSize = GetInt(root, "embeddings", "batch_size", def.BatchSize),
            UseInHook = GetBool(root, "embeddings", "use_in_hook", def.UseInHook),
        };
    }

    // ---- path resolution ------------------------------------------------

    private static string HomeDir() =>
        Environment.GetEnvironmentVariable("USERPROFILE")
        ?? Environment.GetEnvironmentVariable("HOME")
        ?? Directory.GetCurrentDirectory();

    public static string DefaultConfigPath()
    {
        if (OperatingSystem.IsWindows())
        {
            var appData = Environment.GetEnvironmentVariable("APPDATA") ?? HomeDir();
            return Path.Combine(appData, "claude-recall", "config.toml");
        }
        var xdg = Environment.GetEnvironmentVariable("XDG_CONFIG_HOME");
        var baseDir = string.IsNullOrEmpty(xdg)
            ? Path.Combine(HomeDir(), ".config")
            : xdg!;
        return Path.Combine(baseDir, "claude-recall", "config.toml");
    }

    private static string DefaultDbPath()
    {
        if (OperatingSystem.IsWindows())
        {
            var appData = Environment.GetEnvironmentVariable("APPDATA") ?? HomeDir();
            return Path.Combine(appData, "claude-recall", "index.db");
        }
        var xdg = Environment.GetEnvironmentVariable("XDG_CONFIG_HOME");
        var baseDir = string.IsNullOrEmpty(xdg)
            ? Path.Combine(HomeDir(), ".config")
            : xdg!;
        return Path.Combine(baseDir, "claude-recall", "index.db");
    }

    private static string ExpandTilde(string path)
    {
        if (!string.IsNullOrEmpty(path) && path.StartsWith("~", StringComparison.Ordinal))
        {
            var suffix = path.Length > 1 ? path[1..].TrimStart('/', '\\') : string.Empty;
            return string.IsNullOrEmpty(suffix) ? HomeDir() : Path.Combine(HomeDir(), suffix);
        }
        return path;
    }

    // ---- typed getters --------------------------------------------------

    private static TomlTable? Section(TomlTable root, string name)
    {
        return root.TryGetValue(name, out var v) && v is TomlTable t ? t : null;
    }

    private static string GetString(TomlTable root, string section, string key,
                                     string @default, bool expandTilde = false)
    {
        var sec = Section(root, section);
        if (sec == null || !sec.TryGetValue(key, out var v) || v is not string s)
        {
            return @default;
        }
        return expandTilde ? ExpandTilde(s) : s;
    }

    private static int GetInt(TomlTable root, string section, string key, int @default)
    {
        var sec = Section(root, section);
        if (sec == null || !sec.TryGetValue(key, out var v)) return @default;
        return v switch
        {
            long l => (int)l,
            int i => i,
            _ => @default,
        };
    }

    private static double GetDouble(TomlTable root, string section, string key, double @default)
    {
        var sec = Section(root, section);
        if (sec == null || !sec.TryGetValue(key, out var v)) return @default;
        return v switch
        {
            double d => d,
            long l => l,
            int i => i,
            _ => @default,
        };
    }

    private static bool GetBool(TomlTable root, string section, string key, bool @default)
    {
        var sec = Section(root, section);
        if (sec == null || !sec.TryGetValue(key, out var v)) return @default;
        return v is bool b ? b : @default;
    }
}
