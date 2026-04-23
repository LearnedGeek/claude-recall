// Parallel of tests/test_embeddings.py. Uses an injectable HttpMessageHandler
// so no real Ollama contact is required.

using System.Net;
using System.Text;
using System.Text.Json;
using Xunit;

namespace ClaudeRecall.Hook.Tests;

public class VectorsTests
{
    [Fact]
    public void Pack_Unpack_Roundtrip()
    {
        var v = new float[] { 1.0f, -2.5f, 3.14159f, 0f, -0.001f };
        var blob = Vectors.Pack(v);
        Assert.Equal(v.Length * 4, blob.Length);
        var restored = Vectors.Unpack(blob, v.Length);
        Assert.Equal(v, restored);
    }

    [Fact]
    public void Pack_IsLittleEndianFloat32()
    {
        var v = new float[] { 1.0f };
        var blob = Vectors.Pack(v);
        Assert.Equal([0x00, 0x00, 0x80, 0x3f], blob);
    }

    [Fact]
    public void Unpack_RejectsWrongLength()
    {
        Assert.Throws<ArgumentException>(() => Vectors.Unpack(new byte[100], dim: 768));
    }

    [Fact]
    public void CosineMatrix_IdenticalReturnsOne()
    {
        var v = new float[] { 1f, 0f, 0f };
        var result = Vectors.CosineMatrix(v, new[] { v, v, v });
        foreach (var x in result) Assert.Equal(1f, x, precision: 5);
    }

    [Fact]
    public void CosineMatrix_OrthogonalReturnsZero()
    {
        var q = new float[] { 1f, 0f };
        var cs = new[] { new float[] { 0f, 1f }, new float[] { 0f, -1f } };
        var result = Vectors.CosineMatrix(q, cs);
        Assert.Equal(0f, result[0], precision: 5);
        Assert.Equal(0f, result[1], precision: 5);
    }

    [Fact]
    public void CosineMatrix_ZeroQueryReturnsZeros()
    {
        var q = new float[] { 0f, 0f, 0f };
        var result = Vectors.CosineMatrix(q, new[] { new float[] { 1f, 1f, 1f } });
        Assert.Equal(0f, result[0]);
    }

    [Fact]
    public void CosineMatrix_ShapeMismatchReturnsZero()
    {
        var q = new float[] { 1f, 0f };
        var result = Vectors.CosineMatrix(q, new[] { new float[] { 1f, 0f, 0f } });
        Assert.Equal(0f, result[0]);
    }

    [Fact]
    public void CosineMatrix_RanksCloserFirst()
    {
        var q = new float[] { 1f, 0f, 0f };
        var closer = new float[] { 0.9f, 0.1f, 0f };
        var further = new float[] { 0.5f, 0.5f, 0f };
        var scores = Vectors.CosineMatrix(q, new[] { further, closer });
        Assert.True(scores[1] > scores[0]);
    }
}

public class OllamaClientTests
{
    private static OllamaClient ClientWith(HttpMessageHandler handler)
    {
        // The constructor creates its own HttpClient we don't control; use
        // reflection to swap the inner _http for tests. Simpler than wiring
        // an injection seam we don't need in prod.
        var client = new OllamaClient("http://localhost:11434", "nomic-embed-text", 5.0);
        typeof(OllamaClient)
            .GetField("_http", System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.NonPublic)!
            .SetValue(client, new HttpClient(handler) { Timeout = TimeSpan.FromSeconds(5) });
        return client;
    }

    [Fact]
    public void Embed_SingleReturnsVector()
    {
        var handler = new StubHandler(req =>
        {
            Assert.Equal("/api/embed", req.RequestUri?.AbsolutePath);
            return JsonOk("""{"embeddings": [[0.1, 0.2, 0.3]]}""");
        });
        using var c = ClientWith(handler);
        var vec = c.Embed("hello");
        Assert.Equal(new[] { 0.1f, 0.2f, 0.3f }, vec);
    }

    [Fact]
    public void EmbedBatch_PreservesOrder()
    {
        var handler = new StubHandler(req =>
        {
            return JsonOk("""{"embeddings": [[1.0], [2.0], [3.0]]}""");
        });
        using var c = ClientWith(handler);
        var m = c.EmbedBatch(new[] { "a", "b", "c" });
        Assert.Equal(3, m.Length);
        Assert.Equal(1.0f, m[0][0]);
        Assert.Equal(2.0f, m[1][0]);
        Assert.Equal(3.0f, m[2][0]);
    }

    [Fact]
    public void Embed_ThrowsOnHttpError()
    {
        var handler = new StubHandler(_ =>
            new HttpResponseMessage(HttpStatusCode.InternalServerError));
        using var c = ClientWith(handler);
        Assert.Throws<EmbeddingException>(() => c.Embed("x"));
    }

    [Fact]
    public void Embed_ThrowsOnCountMismatch()
    {
        var handler = new StubHandler(_ => JsonOk("""{"embeddings": [[1.0]]}"""));
        using var c = ClientWith(handler);
        Assert.Throws<EmbeddingException>(() => c.EmbedBatch(new[] { "a", "b" }));
    }

    [Fact]
    public void Embed_ThrowsOnNonFinite()
    {
        var handler = new StubHandler(_ => JsonOk("""{"embeddings": [[0.0, 1e999]]}"""));
        using var c = ClientWith(handler);
        Assert.Throws<EmbeddingException>(() => c.Embed("x"));
    }

    [Fact]
    public void Embed_ThrowsOnZeroNorm()
    {
        var handler = new StubHandler(_ => JsonOk("""{"embeddings": [[0.0, 0.0, 0.0]]}"""));
        using var c = ClientWith(handler);
        Assert.Throws<EmbeddingException>(() => c.Embed("x"));
    }

    [Fact]
    public void Probe_HandlesUnreachable()
    {
        var handler = new StubHandler(_ => throw new HttpRequestException("refused"));
        using var c = ClientWith(handler);
        var r = c.Probe();
        Assert.False(r.OllamaReachable);
        Assert.NotNull(r.Error);
    }

    [Fact]
    public void Probe_HandlesModelMissing()
    {
        var handler = new StubHandler(req =>
        {
            if (req.RequestUri!.AbsolutePath == "/api/version")
                return JsonOk("""{"version": "0.18.2"}""");
            if (req.RequestUri.AbsolutePath == "/api/tags")
                return JsonOk("""{"models": [{"name": "llava:latest"}]}""");
            throw new InvalidOperationException(req.RequestUri.AbsolutePath);
        });
        using var c = ClientWith(handler);
        var r = c.Probe();
        Assert.True(r.OllamaReachable);
        Assert.False(r.ModelPresent);
        Assert.Contains("pull", r.Error!);
    }

    [Fact]
    public void Probe_HappyPath()
    {
        var handler = new StubHandler(req =>
        {
            return req.RequestUri!.AbsolutePath switch
            {
                "/api/version" => JsonOk("""{"version": "0.18.2"}"""),
                "/api/tags" => JsonOk("""{"models":[{"name":"nomic-embed-text:latest"}]}"""),
                "/api/embed" => JsonOk("""{"embeddings": [[0.1, 0.2]]}"""),
                _ => throw new InvalidOperationException(req.RequestUri.AbsolutePath),
            };
        });
        using var c = ClientWith(handler);
        var r = c.Probe();
        Assert.True(r.OllamaReachable);
        Assert.True(r.ModelPresent);
        Assert.True(r.EmbedOk);
        Assert.Equal(2, r.Dim);
    }

    // ---- helpers ---------------------------------------------------------

    private static HttpResponseMessage JsonOk(string json) =>
        new(HttpStatusCode.OK)
        {
            Content = new StringContent(json, Encoding.UTF8, "application/json"),
        };

    private sealed class StubHandler : HttpMessageHandler
    {
        private readonly Func<HttpRequestMessage, HttpResponseMessage> _responder;
        public StubHandler(Func<HttpRequestMessage, HttpResponseMessage> responder)
            => _responder = responder;

        protected override HttpResponseMessage Send(HttpRequestMessage req, CancellationToken ct)
            => _responder(req);

        protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage req, CancellationToken ct)
            => Task.FromResult(_responder(req));
    }
}
