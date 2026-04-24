// Embeddings.cs — port of src/claude_recall/embeddings.py.
//
// Ollama HTTP client (via HttpClient), little-endian float32 BLOB codec
// matching Python's pack_vector, and vectorized cosine similarity over a
// candidate matrix.

using System.Buffers.Binary;
using System.Net.Http.Json;
using System.Numerics;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace ClaudeRecall.Hook;

internal sealed class EmbeddingException : Exception
{
    public EmbeddingException(string message) : base(message) { }
    public EmbeddingException(string message, Exception inner) : base(message, inner) { }
}

internal sealed record ProbeResult(
    bool OllamaReachable,
    string? Version,
    bool ModelPresent,
    bool EmbedOk,
    int? Dim,
    string? Error
);

internal class OllamaClient : IDisposable
{
    public const int DefaultDim = 768;

    private readonly HttpClient _http;
    private readonly string _baseUrl;
    private readonly string _model;
    private readonly string? _keepAlive;

    public OllamaClient(
        string baseUrl,
        string model,
        double timeoutSeconds,
        string? keepAlive = null)
    {
        _baseUrl = baseUrl.TrimEnd('/');
        _model = model;
        _keepAlive = keepAlive;  // issue #11 — sent on every embed call
        _http = new HttpClient
        {
            Timeout = TimeSpan.FromSeconds(timeoutSeconds),
        };
    }

    public void Dispose() => _http.Dispose();

    /// <summary>Embed a single string. Returns a float array of length ``dim``.</summary>
    public float[] Embed(string text)
    {
        var batch = EmbedBatch(new[] { text });
        return batch[0];
    }

    /// <summary>Embed a batch of strings. Preserves input order. Validates shape + finite + non-zero norm.</summary>
    public float[][] EmbedBatch(IReadOnlyList<string> texts)
    {
        if (texts.Count == 0)
        {
            return Array.Empty<float[]>();
        }

        EmbedResponse? body;
        try
        {
            using var req = new HttpRequestMessage(HttpMethod.Post, $"{_baseUrl}/api/embed")
            {
                Content = JsonContent.Create(
                    new EmbedRequest(_model, texts.ToArray(), _keepAlive),
                    EmbeddingsJsonContext.Default.EmbedRequest),
            };
            using var resp = _http.Send(req, HttpCompletionOption.ResponseContentRead);
            resp.EnsureSuccessStatusCode();
            using var stream = resp.Content.ReadAsStream();
            body = JsonSerializer.Deserialize(stream, EmbeddingsJsonContext.Default.EmbedResponse);
        }
        catch (HttpRequestException ex)
        {
            throw new EmbeddingException($"Ollama request failed: {ex.Message}", ex);
        }
        catch (TaskCanceledException ex)
        {
            throw new EmbeddingException($"Ollama request timed out: {ex.Message}", ex);
        }
        catch (JsonException ex)
        {
            throw new EmbeddingException($"Ollama returned non-JSON: {ex.Message}", ex);
        }

        if (body?.Embeddings is null || body.Embeddings.Length != texts.Count)
        {
            throw new EmbeddingException(
                $"Ollama returned {(body?.Embeddings?.Length.ToString() ?? "?")} embeddings for {texts.Count} inputs");
        }

        var matrix = new float[body.Embeddings.Length][];
        for (int i = 0; i < body.Embeddings.Length; i++)
        {
            var row = body.Embeddings[i];
            if (row is null)
            {
                throw new EmbeddingException("Ollama returned null embedding row");
            }
            double sumSq = 0;
            for (int j = 0; j < row.Length; j++)
            {
                var v = row[j];
                if (!float.IsFinite(v))
                {
                    throw new EmbeddingException("Ollama returned non-finite values");
                }
                sumSq += v * v;
            }
            if (sumSq == 0.0)
            {
                throw new EmbeddingException("Ollama returned zero-norm vector");
            }
            matrix[i] = row;
        }
        return matrix;
    }

    /// <summary>Diagnose the full Ollama path without throwing. Mirrors Python probe().</summary>
    public ProbeResult Probe()
    {
        string? version = null;
        try
        {
            using var resp = _http.Send(
                new HttpRequestMessage(HttpMethod.Get, $"{_baseUrl}/api/version"));
            resp.EnsureSuccessStatusCode();
            using var s = resp.Content.ReadAsStream();
            var v = JsonSerializer.Deserialize(s, EmbeddingsJsonContext.Default.VersionResponse);
            version = v?.Version;
        }
        catch (Exception ex) when (ex is HttpRequestException or TaskCanceledException or JsonException)
        {
            return new ProbeResult(false, null, false, false, null,
                $"cannot reach Ollama at {_baseUrl}: {ex.Message}");
        }

        try
        {
            using var resp = _http.Send(
                new HttpRequestMessage(HttpMethod.Get, $"{_baseUrl}/api/tags"));
            resp.EnsureSuccessStatusCode();
            using var s = resp.Content.ReadAsStream();
            var tags = JsonSerializer.Deserialize(s, EmbeddingsJsonContext.Default.TagsResponse);
            var names = tags?.Models?.Select(m => (m.Name ?? string.Empty).Split(':')[0]).ToHashSet(
                StringComparer.OrdinalIgnoreCase) ?? new HashSet<string>();
            var modelBase = _model.Split(':')[0];
            if (!names.Contains(modelBase))
            {
                return new ProbeResult(true, version, false, false, null,
                    $"model '{_model}' not pulled. Run: ollama pull {_model}");
            }
        }
        catch (Exception ex) when (ex is HttpRequestException or TaskCanceledException or JsonException)
        {
            return new ProbeResult(true, version, false, false, null,
                $"cannot list Ollama models: {ex.Message}");
        }

        try
        {
            var vec = Embed("probe");
            return new ProbeResult(true, version, true, true, vec.Length, null);
        }
        catch (EmbeddingException ex)
        {
            return new ProbeResult(true, version, true, false, null,
                $"embed probe failed: {ex.Message}");
        }
    }
}

// ---- BLOB codec + cosine --------------------------------------------------

internal static class Vectors
{
    /// <summary>Serialize a float vector as little-endian float32 bytes.</summary>
    public static byte[] Pack(float[] v)
    {
        var bytes = new byte[v.Length * sizeof(float)];
        for (int i = 0; i < v.Length; i++)
        {
            BinaryPrimitives.WriteSingleLittleEndian(
                bytes.AsSpan(i * sizeof(float), sizeof(float)), v[i]);
        }
        return bytes;
    }

    /// <summary>Deserialize a BLOB produced by Pack back to a float array.</summary>
    public static float[] Unpack(ReadOnlySpan<byte> blob, int dim)
    {
        if (blob.Length != dim * sizeof(float))
        {
            throw new ArgumentException(
                $"blob length {blob.Length} does not match expected {dim * sizeof(float)} for dim={dim}");
        }
        var v = new float[dim];
        for (int i = 0; i < dim; i++)
        {
            v[i] = BinaryPrimitives.ReadSingleLittleEndian(
                blob.Slice(i * sizeof(float), sizeof(float)));
        }
        return v;
    }

    /// <summary>
    /// Cosine similarity between a query vector and each row of the candidate
    /// matrix. Uses Vector&lt;float&gt; SIMD where available.
    /// </summary>
    public static float[] CosineMatrix(float[] query, IReadOnlyList<float[]> candidates)
    {
        if (query.Length == 0 || candidates.Count == 0)
        {
            return new float[candidates.Count];
        }
        var qNorm = MathF.Sqrt(DotSelf(query));
        var result = new float[candidates.Count];
        if (qNorm == 0f)
        {
            return result;
        }
        for (int i = 0; i < candidates.Count; i++)
        {
            var c = candidates[i];
            if (c.Length != query.Length)
            {
                result[i] = 0f;
                continue;
            }
            var cNorm = MathF.Sqrt(DotSelf(c));
            if (cNorm == 0f)
            {
                result[i] = 0f;
                continue;
            }
            var dot = Dot(query, c);
            result[i] = dot / (qNorm * cNorm);
        }
        return result;
    }

    private static float Dot(float[] a, float[] b)
    {
        int n = a.Length;
        int simdWidth = Vector<float>.Count;
        float sum = 0f;
        int i = 0;
        if (Vector.IsHardwareAccelerated && n >= simdWidth)
        {
            var acc = Vector<float>.Zero;
            for (; i <= n - simdWidth; i += simdWidth)
            {
                var va = new Vector<float>(a, i);
                var vb = new Vector<float>(b, i);
                acc += va * vb;
            }
            sum = Vector.Sum(acc);
        }
        for (; i < n; i++)
        {
            sum += a[i] * b[i];
        }
        return sum;
    }

    private static float DotSelf(float[] a)
    {
        int n = a.Length;
        int simdWidth = Vector<float>.Count;
        float sum = 0f;
        int i = 0;
        if (Vector.IsHardwareAccelerated && n >= simdWidth)
        {
            var acc = Vector<float>.Zero;
            for (; i <= n - simdWidth; i += simdWidth)
            {
                var va = new Vector<float>(a, i);
                acc += va * va;
            }
            sum = Vector.Sum(acc);
        }
        for (; i < n; i++)
        {
            sum += a[i] * a[i];
        }
        return sum;
    }
}

// ---- JSON contracts (source-generated for AOT) ----------------------------

internal sealed class EmbedRequest
{
    [JsonPropertyName("model")] public string Model { get; init; }
    [JsonPropertyName("input")] public string[] Input { get; init; }
    // Issue #11: null values get omitted by System.Text.Json defaults
    // (DefaultIgnoreCondition = WhenWritingNull on the context below), so
    // keep_alive is only sent when the client was configured with one.
    [JsonPropertyName("keep_alive")] public string? KeepAlive { get; init; }

    public EmbedRequest(string model, string[] input, string? keepAlive = null)
    {
        Model = model;
        Input = input;
        KeepAlive = keepAlive;
    }
}

internal sealed class EmbedResponse
{
    [JsonPropertyName("embeddings")] public float[]?[]? Embeddings { get; set; }
}

internal sealed class VersionResponse
{
    [JsonPropertyName("version")] public string? Version { get; set; }
}

internal sealed class TagsResponse
{
    [JsonPropertyName("models")] public TagModel[]? Models { get; set; }
}

internal sealed class TagModel
{
    [JsonPropertyName("name")] public string? Name { get; set; }
}

[JsonSourceGenerationOptions(
    // Drop keep_alive from the serialized JSON when null, so the existing
    // contract (no keep_alive field = Ollama default 5m) is preserved for
    // clients that don't set one. Issue #11.
    DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull)]
[JsonSerializable(typeof(EmbedRequest))]
[JsonSerializable(typeof(EmbedResponse))]
[JsonSerializable(typeof(VersionResponse))]
[JsonSerializable(typeof(TagsResponse))]
[JsonSerializable(typeof(TagModel))]
internal partial class EmbeddingsJsonContext : JsonSerializerContext
{
}
