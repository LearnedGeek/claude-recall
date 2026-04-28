// claude-recall-hook — UserPromptSubmit hook binary (v0.4).
//
// Single-file NativeAOT exe that replaces the Python-CLI-based hook path.
// Reads {"prompt": "..."} on stdin, writes the wrapped Claude Code hook
// envelope on stdout:
//
//   {"hookSpecificOutput":
//     {"hookEventName":"UserPromptSubmit","additionalContext":"..."}}
//
// or `{}` when there are no matches. Always exits 0 — Claude Code hook
// contract requires non-blocking. Issue #21 (v0.6.4): switched from the
// legacy top-level `additionalContext` shape; the strict-validation pass
// alongside Claude Code v2.1.118 silently drops the top-level form.
//
// See docs/HOOK-BINARY-PLAN.md for the full design.

using System.Diagnostics;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace ClaudeRecall.Hook;

internal static class Program
{
    // Issue #12: read from assembly metadata so the build pipeline's
    // `/p:Version=X.Y.Z` flows through to --version output automatically.
    // No more hand-bumping a constant that drifts from the csproj.
    public static readonly string Version = GetAssemblyVersion();

    private static string GetAssemblyVersion()
    {
        var asm = typeof(Program).Assembly;
        // AssemblyInformationalVersion respects the <Version> csproj property
        // as-is (e.g. "0.5.2" not "0.5.2.0"), so prefer it over AssemblyVersion.
        var info = asm.GetCustomAttributes(typeof(System.Reflection.AssemblyInformationalVersionAttribute), false);
        if (info.Length > 0)
        {
            var v = ((System.Reflection.AssemblyInformationalVersionAttribute)info[0]).InformationalVersion;
            // Strip any git-hash suffix setuptools-scm-style tools append.
            var plus = v.IndexOf('+');
            return plus >= 0 ? v[..plus] : v;
        }
        return asm.GetName().Version?.ToString(3) ?? "unknown";
    }

    public static int Main(string[] args)
    {
        try
        {
            return Run(args);
        }
        catch (Exception ex)
        {
            // Hook failsafe: any unhandled exception collapses to empty output + exit 0.
            // Never block Claude Code.
            try { Console.Error.WriteLine($"claude-recall-hook: unhandled error: {ex.Message}"); }
            catch { /* stderr failures are also swallowed */ }
            Console.WriteLine("{}");
            return 0;
        }
    }

    private static int Run(string[] args)
    {
        var stopwatch = Stopwatch.StartNew();
        var parsed = CliArgs.Parse(args);

        if (parsed.ShowVersion)
        {
            Console.WriteLine($"claude-recall-hook {Version}");
            return 0;
        }

        var cfg = Config.Load(parsed.ConfigPath);
        // Issue #11: per-stage timing breakdown to stderr when --timing is
        // set. Users can diagnose which phase (startup, DB, Ollama embed,
        // rerank, JSON) is spending the time instead of guessing.
        var timings = parsed.Timing ? new List<(string, long)>() : null;
        void Stamp(string stage)
        {
            timings?.Add((stage, stopwatch.ElapsedMilliseconds));
        }
        Stamp("startup+config");

        if (parsed.Probe)
        {
            RunProbe(cfg);
            return 0;
        }

        // ---- Read stdin ---------------------------------------------------
        var prompt = ReadPromptFromStdin();
        Stamp("stdin");
        if (string.IsNullOrWhiteSpace(prompt))
        {
            Console.WriteLine("{}");
            return 0;
        }

        // ---- Keyword-extracted FTS query ---------------------------------
        var keywords = Keywords.Extract(prompt);
        var ftsQuery = Keywords.BuildFtsQuery(keywords);
        if (string.IsNullOrWhiteSpace(ftsQuery))
        {
            // Nothing survived extraction; fall back to raw prompt.
            ftsQuery = prompt;
        }
        Stamp("keywords");

        // ---- Open DB read-only -------------------------------------------
        if (!File.Exists(cfg.DbPath))
        {
            if (parsed.Verbose) Console.Error.WriteLine($"hook: db not found: {cfg.DbPath}");
            Console.WriteLine("{}");
            return 0;
        }

        using var conn = Storage.OpenReadOnly(cfg.DbPath);
        Stamp("db-open");

        // ---- Project scope (cwd → slug) ----------------------------------
        var projectSlug = Projects.ResolveProjectSlug(conn, Environment.CurrentDirectory);
        Stamp("slug-resolve");

        // ---- FTS5 pool ---------------------------------------------------
        var poolSize = Math.Max(cfg.HookLimit, cfg.RerankPoolSize);
        var pool = Storage.SearchFts5WithFallback(
            conn,
            rawQuery: ftsQuery,
            sanitizedQuery: ftsQuery,  // already OR-joined from BuildFtsQuery
            projectSlug: projectSlug,
            days: cfg.HookDays,
            poolSize: poolSize);
        Stamp("fts5");

        // Apply threshold.
        var gated = pool.Where(r => r.Score >= cfg.HookThreshold).ToList();

        // ---- Semantic rerank (optional) ----------------------------------
        IReadOnlyList<RerankedResult> ranked;
        if (!parsed.NoSemantic && cfg.EmbeddingsEnabled && cfg.UseInHook && gated.Count > 0)
        {
            var timeoutSec = parsed.TimeoutMs.HasValue
                ? parsed.TimeoutMs.Value / 1000.0
                : cfg.RequestTimeoutSeconds;
            using var client = new OllamaClient(
                cfg.OllamaBaseUrl, cfg.EmbeddingsModel, timeoutSec, cfg.KeepAlive);
            var (rerankedList, reason) = Rerank.Run(conn, client, ftsQuery, gated);
            ranked = rerankedList;
            if (parsed.Verbose && reason is not null)
            {
                Console.Error.WriteLine($"hook: semantic fallback — {reason}");
            }
        }
        else
        {
            // No rerank — just wrap the gated FTS rows as RerankedResults for uniform formatting.
            var asReranked = new RerankedResult[gated.Count];
            for (int i = 0; i < gated.Count; i++)
            {
                asReranked[i] = new RerankedResult(gated[i], gated[i].Score, null);
            }
            ranked = asReranked;
        }
        Stamp("rerank");

        // ---- Emit agent-context JSON --------------------------------------
        var top = ranked.Take(cfg.HookLimit).ToList();
        WriteAgentContext(top);
        Stamp("json-out");

        if (timings is not null)
        {
            long prev = 0;
            Console.Error.WriteLine("claude-recall-hook timing (ms cumulative / delta):");
            foreach (var (stage, ms) in timings)
            {
                var delta = ms - prev;
                Console.Error.WriteLine($"  {stage,-18} {ms,6} ms  (+{delta} ms)");
                prev = ms;
            }
        }

        if (parsed.Verbose)
        {
            Console.Error.WriteLine(
                $"hook: {stopwatch.ElapsedMilliseconds}ms  pool={pool.Count}  gated={gated.Count}  returned={top.Count}");
        }
        return 0;
    }

    // ---- helpers ---------------------------------------------------------

    private static string ReadPromptFromStdin()
    {
        if (Console.IsInputRedirected == false)
        {
            return string.Empty;
        }
        string raw;
        try { raw = Console.In.ReadToEnd(); }
        catch { return string.Empty; }

        if (string.IsNullOrWhiteSpace(raw))
        {
            return string.Empty;
        }

        try
        {
            using var doc = JsonDocument.Parse(raw);
            if (doc.RootElement.ValueKind == JsonValueKind.Object
                && doc.RootElement.TryGetProperty("prompt", out var p)
                && p.ValueKind == JsonValueKind.String)
            {
                return p.GetString() ?? string.Empty;
            }
        }
        catch (JsonException) { /* fall through */ }
        return string.Empty;
    }

    private static void WriteAgentContext(IReadOnlyList<RerankedResult> results)
    {
        if (results.Count == 0)
        {
            Console.WriteLine("{}");
            return;
        }

        // Use \n explicitly (not Environment.NewLine) so the serialized JSON
        // matches the Python CLI's additionalContext byte-for-byte on every OS.
        var sb = new System.Text.StringBuilder();
        sb.Append("Relevant prior-session context (claude-recall):\n\n");
        for (int i = 0; i < results.Count; i++)
        {
            var r = results[i];
            var date = string.IsNullOrEmpty(r.Row.StartedAt) ? string.Empty : r.Row.StartedAt[..Math.Min(10, r.Row.StartedAt.Length)];
            var shortId = r.Row.SessionId[..Math.Min(8, r.Row.SessionId.Length)];
            var snippet = r.Row.Snippet.Replace(Storage.SnippetStart, "").Replace(Storage.SnippetEnd, "");
            sb.Append($"[Session {shortId}, {date}] {r.Row.Role}: {snippet}");
            if (i < results.Count - 1) sb.Append('\n');
        }

        // Issue #21: wrapped form. See Program.cs header comment for the
        // strict-validation backstory.
        var payload = new AgentContextEnvelope
        {
            HookSpecificOutput = new HookSpecificOutput
            {
                HookEventName = "UserPromptSubmit",
                AdditionalContext = sb.ToString(),
            },
        };
        var json = JsonSerializer.Serialize(payload, ProgramJsonContext.Default.AgentContextEnvelope);
        Console.WriteLine(json);
    }

    private static void RunProbe(Config cfg)
    {
        if (!cfg.EmbeddingsEnabled)
        {
            Console.WriteLine("{\"embeddings_enabled\": false}");
            return;
        }
        using var client = new OllamaClient(
            cfg.OllamaBaseUrl, cfg.EmbeddingsModel,
            Math.Min(cfg.RequestTimeoutSeconds, 5.0),
            cfg.KeepAlive);
        var probe = client.Probe();
        var json = JsonSerializer.Serialize(probe, ProgramJsonContext.Default.ProbeResult);
        Console.WriteLine(json);
    }
}

internal sealed class CliArgs
{
    public string? ConfigPath { get; private set; }
    public bool ShowVersion { get; private set; }
    public bool Probe { get; private set; }
    public bool NoSemantic { get; private set; }
    public bool Verbose { get; private set; }
    // Issue #11: emit per-stage timing to stderr for latency self-diagnosis.
    // Implies verbose behavior; separate flag because `--verbose` prints
    // diagnostic info beyond timing that users may not want in a hook log.
    public bool Timing { get; private set; }
    public int? TimeoutMs { get; private set; }

    public static CliArgs Parse(string[] args)
    {
        var result = new CliArgs();
        for (int i = 0; i < args.Length; i++)
        {
            var a = args[i];
            switch (a)
            {
                case "--version": result.ShowVersion = true; break;
                case "--probe": result.Probe = true; break;
                case "--no-semantic": result.NoSemantic = true; break;
                case "--verbose": result.Verbose = true; break;
                case "--timing": result.Timing = true; break;
                case "--config":
                    if (i + 1 < args.Length) result.ConfigPath = args[++i];
                    break;
                case "--timeout-ms":
                    if (i + 1 < args.Length && int.TryParse(args[++i], out var ms))
                    {
                        result.TimeoutMs = ms;
                    }
                    break;
                default:
                    // Unknown flags silently ignored — hooks shouldn't error on argv noise.
                    break;
            }
        }
        return result;
    }
}

internal sealed class AgentContextEnvelope
{
    // Issue #21 (v0.6.4): wrapped under hookSpecificOutput per Claude Code's
    // hook output schema. The legacy top-level `additionalContext` form is
    // silently dropped by the strict-validation pass.
    [JsonPropertyName("hookSpecificOutput")] public HookSpecificOutput HookSpecificOutput { get; set; } = new();
}

internal sealed class HookSpecificOutput
{
    [JsonPropertyName("hookEventName")] public string HookEventName { get; set; } = "UserPromptSubmit";
    [JsonPropertyName("additionalContext")] public string AdditionalContext { get; set; } = string.Empty;
}

[JsonSerializable(typeof(AgentContextEnvelope))]
[JsonSerializable(typeof(HookSpecificOutput))]
[JsonSerializable(typeof(ProbeResult))]
internal partial class ProgramJsonContext : JsonSerializerContext
{
}
