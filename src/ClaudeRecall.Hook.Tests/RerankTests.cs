// Parallel of tests/test_semantic_search.py. Verifies hybrid retrieval
// flips order when BM25 and semantic disagree.

using Microsoft.Data.Sqlite;
using Xunit;

namespace ClaudeRecall.Hook.Tests;

public class RerankTests : IDisposable
{
    private readonly string _dbPath;

    public RerankTests()
    {
        _dbPath = Path.Combine(Path.GetTempPath(), $"crhook-rerank-{Guid.NewGuid():N}.db");
        SQLitePCL.Batteries_V2.Init();
        Seed(_dbPath);
    }

    public void Dispose()
    {
        SqliteConnection.ClearAllPools();
        if (File.Exists(_dbPath))
        {
            try { File.Delete(_dbPath); } catch (IOException) { }
        }
    }

    [Fact]
    public void Run_SemanticOrderingOverridesBM25()
    {
        using var conn = Storage.OpenReadOnly(_dbPath);
        var pool = Storage.SearchFts5(conn, "cascade", null, 3650, 50);
        Assert.True(pool.Count >= 3);

        // Query vector is the direction of msg 1 (architecture). msg 2 is orthogonal.
        var queryVec = new float[] { 1f, 0f, 0f, 0f };
        using var client = new ScriptedClient(queryVec);

        var (results, reason) = Rerank.Run(conn, client, "cascade", pool);
        Assert.Null(reason);
        Assert.NotEmpty(results);
        // Top result should have the architecture content.
        Assert.Contains("architecture", results[0].Row.Content.ToLowerInvariant());
        Assert.NotNull(results[0].Cosine);
    }

    [Fact]
    public void Run_FallsBackWhenNoVectors()
    {
        // Build a separate DB with messages but no vector rows.
        var bareDb = Path.Combine(Path.GetTempPath(), $"bare-{Guid.NewGuid():N}.db");
        try
        {
            SeedBareCorpus(bareDb);
            using var conn = Storage.OpenReadOnly(bareDb);
            var pool = Storage.SearchFts5(conn, "cascade", null, 3650, 50);
            using var client = new ScriptedClient(new float[] { 1f, 0f });

            var (results, reason) = Rerank.Run(conn, client, "cascade", pool);
            Assert.Equal(pool.Count, results.Count);
            Assert.Contains("no vectors", reason!);
            Assert.True(results.All(r => r.Cosine is null));
        }
        finally
        {
            SqliteConnection.ClearAllPools();
            if (File.Exists(bareDb)) File.Delete(bareDb);
        }
    }

    [Fact]
    public void Run_FallsBackWhenOllamaFails()
    {
        using var conn = Storage.OpenReadOnly(_dbPath);
        var pool = Storage.SearchFts5(conn, "cascade", null, 3650, 50);
        using var failing = new FailingClient();

        var (results, reason) = Rerank.Run(conn, failing, "cascade", pool);
        Assert.Equal(pool.Count, results.Count);
        Assert.Contains("Ollama embed failed", reason!);
    }

    [Fact]
    public void Run_EmptyPoolShortCircuits()
    {
        using var conn = Storage.OpenReadOnly(_dbPath);
        using var client = new ScriptedClient(new float[] { 1f, 0f });
        var (results, reason) = Rerank.Run(conn, client, "x", Array.Empty<FtsResult>());
        Assert.Empty(results);
        Assert.Null(reason);
    }

    // ---- fixtures --------------------------------------------------------

    private static void Seed(string path)
    {
        var cs = new SqliteConnectionStringBuilder { DataSource = path }.ToString();
        using var conn = new SqliteConnection(cs);
        conn.Open();
        using (var cmd = conn.CreateCommand())
        {
            cmd.CommandText = Schema + """
                INSERT INTO sessions VALUES ('s1','proj','/tmp/a.jsonl',1.0,'2026-04-20T10:00:00Z','2026-04-20T10:02:00Z',3,'2026-04-23T00:00:00Z');
                INSERT INTO messages(msg_id, session_id, role, content, turn_index, timestamp) VALUES
                  (1, 's1', 'user', 'cascade feedback-loop architecture', 0, '2026-04-20T10:00:00Z'),
                  (2, 's1', 'user', 'cascade of car problems in the parking lot', 1, '2026-04-20T10:01:00Z'),
                  (3, 's1', 'user', 'cascade styling concerns for the UI', 2, '2026-04-20T10:02:00Z');
                """;
            cmd.ExecuteNonQuery();
        }
        // Seed vectors: msg 1 aligned with [1,0,0,0]; msg 2 orthogonal; msg 3 partial.
        InsertVector(conn, 1, new float[] { 0.95f, 0.05f, 0f, 0f });
        InsertVector(conn, 2, new float[] { 0.10f, 0.99f, 0f, 0f });
        InsertVector(conn, 3, new float[] { 0.50f, 0.50f, 0f, 0f });
    }

    private static void SeedBareCorpus(string path)
    {
        var cs = new SqliteConnectionStringBuilder { DataSource = path }.ToString();
        using var conn = new SqliteConnection(cs);
        conn.Open();
        using var cmd = conn.CreateCommand();
        cmd.CommandText = Schema + """
            INSERT INTO sessions VALUES ('s1','proj','/tmp/a.jsonl',1.0,'2026-04-20T10:00:00Z','2026-04-20T10:01:00Z',2,'2026-04-23T00:00:00Z');
            INSERT INTO messages(msg_id, session_id, role, content, turn_index, timestamp) VALUES
              (1, 's1', 'user', 'cascade architecture', 0, '2026-04-20T10:00:00Z'),
              (2, 's1', 'user', 'cascade styling', 1, '2026-04-20T10:01:00Z');
            """;
        cmd.ExecuteNonQuery();
    }

    private static void InsertVector(SqliteConnection conn, long msgId, float[] v)
    {
        using var cmd = conn.CreateCommand();
        cmd.CommandText = @"
            INSERT INTO message_vectors(msg_id, vector, model, dim, embedded_at)
            VALUES ($id, $blob, 'test', $dim, '2026-04-23T00:00:00Z')";
        cmd.Parameters.AddWithValue("$id", msgId);
        cmd.Parameters.AddWithValue("$blob", Vectors.Pack(v));
        cmd.Parameters.AddWithValue("$dim", v.Length);
        cmd.ExecuteNonQuery();
    }

    private const string Schema = """
        CREATE TABLE sessions (
          session_id TEXT PRIMARY KEY,
          project_slug TEXT NOT NULL,
          file_path TEXT NOT NULL UNIQUE,
          file_mtime REAL NOT NULL,
          started_at TEXT, ended_at TEXT,
          turn_count INTEGER NOT NULL DEFAULT 0,
          indexed_at TEXT NOT NULL);
        CREATE TABLE messages (
          msg_id INTEGER PRIMARY KEY AUTOINCREMENT,
          session_id TEXT NOT NULL,
          role TEXT NOT NULL,
          content TEXT NOT NULL,
          turn_index INTEGER NOT NULL,
          timestamp TEXT,
          FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE);
        CREATE VIRTUAL TABLE messages_fts USING fts5(
          content, content='messages', content_rowid='msg_id', tokenize='porter unicode61');
        CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN
          INSERT INTO messages_fts(rowid, content) VALUES (new.msg_id, new.content);
        END;
        CREATE TABLE message_vectors (
          msg_id INTEGER PRIMARY KEY,
          vector BLOB NOT NULL, model TEXT NOT NULL, dim INTEGER NOT NULL,
          embedded_at TEXT NOT NULL,
          FOREIGN KEY (msg_id) REFERENCES messages(msg_id) ON DELETE CASCADE);
        """;

    // ---- mock clients -----------------------------------------------------
    // Both reflection-swap the inner HttpClient so Embed() goes through a
    // stubbed HTTP handler instead of real Ollama.

    private static void SwapHttp(OllamaClient client, HttpMessageHandler handler)
    {
        typeof(OllamaClient)
            .GetField("_http", System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.NonPublic)!
            .SetValue(client, new HttpClient(handler) { Timeout = TimeSpan.FromSeconds(5) });
    }

    private sealed class ScriptedClient : OllamaClient
    {
        public ScriptedClient(float[] queryVec) : base("http://localhost:0", "stub", 1.0)
        {
            SwapHttp(this, new FixedVectorHandler(queryVec));
        }

        private sealed class FixedVectorHandler : HttpMessageHandler
        {
            private readonly float[] _vec;
            public FixedVectorHandler(float[] vec) => _vec = vec;

            private HttpResponseMessage Respond(HttpRequestMessage _)
            {
                var floats = string.Join(",", _vec.Select(f => f.ToString(System.Globalization.CultureInfo.InvariantCulture)));
                var body = $"{{\"embeddings\":[[{floats}]]}}";
                return new HttpResponseMessage(System.Net.HttpStatusCode.OK)
                {
                    Content = new StringContent(body, System.Text.Encoding.UTF8, "application/json"),
                };
            }

            protected override HttpResponseMessage Send(HttpRequestMessage req, CancellationToken ct) => Respond(req);
            protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage req, CancellationToken ct)
                => Task.FromResult(Respond(req));
        }
    }

    private sealed class FailingClient : OllamaClient
    {
        public FailingClient() : base("http://localhost:0", "stub", 0.1)
        {
            SwapHttp(this, new ThrowingHandler());
        }

        private sealed class ThrowingHandler : HttpMessageHandler
        {
            protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage req, CancellationToken ct)
                => throw new HttpRequestException("simulated outage");
            protected override HttpResponseMessage Send(HttpRequestMessage req, CancellationToken ct)
                => throw new HttpRequestException("simulated outage");
        }
    }
}
