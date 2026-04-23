using Microsoft.Data.Sqlite;
using Xunit;

namespace ClaudeRecall.Hook.Tests;

public class StorageTests : IDisposable
{
    private readonly string _dbPath;

    public StorageTests()
    {
        _dbPath = Path.Combine(Path.GetTempPath(), $"crhook-storage-{Guid.NewGuid():N}.db");
        SQLitePCL.Batteries_V2.Init();
        SeedDatabase(_dbPath);
    }

    public void Dispose()
    {
        // Microsoft.Data.Sqlite pools connections; the pool keeps the file
        // locked after our `using` blocks close. ClearAllPools releases it.
        SqliteConnection.ClearAllPools();
        if (File.Exists(_dbPath))
        {
            try { File.Delete(_dbPath); } catch (IOException) { /* CI cleanup */ }
        }
    }

    [Fact]
    public void OpenReadOnly_Succeeds()
    {
        using var conn = Storage.OpenReadOnly(_dbPath);
        Assert.Equal(System.Data.ConnectionState.Open, conn.State);
    }

    [Fact]
    public void SearchFts5_ReturnsMatchingRows()
    {
        using var conn = Storage.OpenReadOnly(_dbPath);
        var rows = Storage.SearchFts5(conn, "regex", projectSlug: null, days: 3650, poolSize: 50);
        Assert.NotEmpty(rows);
        Assert.Contains(rows, r => r.Content.Contains("regex", StringComparison.OrdinalIgnoreCase));
    }

    [Fact]
    public void SearchFts5_SnippetWrapsMatchesInMark()
    {
        using var conn = Storage.OpenReadOnly(_dbPath);
        var rows = Storage.SearchFts5(conn, "regex", null, 3650, 50);
        Assert.Contains(rows, r => r.Snippet.Contains("<mark>") && r.Snippet.Contains("</mark>"));
    }

    [Fact]
    public void SearchFts5_BM25ScoresSortedHighFirst()
    {
        using var conn = Storage.OpenReadOnly(_dbPath);
        var rows = Storage.SearchFts5(conn, "regex", null, 3650, 50);
        for (int i = 1; i < rows.Count; i++)
        {
            Assert.True(rows[i - 1].Score >= rows[i].Score);
        }
    }

    [Fact]
    public void SearchFts5_ProjectScopeFiltersResults()
    {
        using var conn = Storage.OpenReadOnly(_dbPath);
        var rows = Storage.SearchFts5(conn, "regex", "no-such-project", 3650, 50);
        Assert.Empty(rows);
    }

    [Fact]
    public void SearchFts5_DaysFilterExcludesOld()
    {
        using var conn = Storage.OpenReadOnly(_dbPath);
        // Fixture rows are dated 2026-04-21; days=0 → cutoff is "now", so none.
        var rows = Storage.SearchFts5(conn, "regex", null, 0, 50);
        Assert.Empty(rows);
    }

    [Fact]
    public void SearchFts5WithFallback_RetriesOnEmpty()
    {
        using var conn = Storage.OpenReadOnly(_dbPath);
        // AND of all tokens → zero hits; OR-joined fallback should find at least one.
        var rows = Storage.SearchFts5WithFallback(
            conn, rawQuery: "remind me what we decided about regex patterns",
            sanitizedQuery: "remind OR decided OR regex OR patterns",
            projectSlug: null, days: 3650, poolSize: 50);
        Assert.NotEmpty(rows);
    }

    [Fact]
    public void LoadVectors_ReturnsSeededRows()
    {
        using var conn = Storage.OpenReadOnly(_dbPath);
        // Seed includes msg_id=1 with a deterministic 4-byte blob.
        var result = Storage.LoadVectors(conn, new long[] { 1, 2, 9999 });
        Assert.True(result.ContainsKey(1));
        Assert.Equal(1, result[1].Dim);
        Assert.False(result.ContainsKey(9999));
    }

    // ---- fixture builder -------------------------------------------------

    /// <summary>
    /// Build a small SQLite file with the real claude-recall schema so we
    /// exercise FTS5 + triggers against actual shape, not a mock.
    /// </summary>
    private static void SeedDatabase(string path)
    {
        var cs = new SqliteConnectionStringBuilder { DataSource = path }.ToString();
        using var conn = new SqliteConnection(cs);
        conn.Open();
        using (var cmd = conn.CreateCommand())
        {
            cmd.CommandText = @"
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
                  content,
                  content='messages',
                  content_rowid='msg_id',
                  tokenize='porter unicode61'
                );

                CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN
                  INSERT INTO messages_fts(rowid, content) VALUES (new.msg_id, new.content);
                END;

                CREATE TABLE message_vectors (
                  msg_id INTEGER PRIMARY KEY,
                  vector BLOB NOT NULL,
                  model TEXT NOT NULL,
                  dim INTEGER NOT NULL,
                  embedded_at TEXT NOT NULL,
                  FOREIGN KEY (msg_id) REFERENCES messages(msg_id) ON DELETE CASCADE);

                INSERT INTO sessions(session_id, project_slug, file_path, file_mtime, started_at, turn_count, indexed_at)
                  VALUES ('fixture-short-001', 'test-project', '/tmp/a.jsonl', 1.0, '2026-04-21T14:32:10Z', 3, '2026-04-23T00:00:00Z');
                INSERT INTO messages(msg_id, session_id, role, content, turn_index, timestamp) VALUES
                  (1, 'fixture-short-001', 'user',      'We decided against using regex for this detection work three weeks ago.', 0, '2026-04-21T14:32:10Z'),
                  (2, 'fixture-short-001', 'assistant', 'The principle we established is architecture-over-instruction. Regex was fragile.', 1, '2026-04-21T14:32:15Z'),
                  (3, 'fixture-short-001', 'user',      'Right. So we went with the LLM claim extraction instead. That was Feature 14 v2.', 2, '2026-04-21T14:33:02Z');

                INSERT INTO message_vectors(msg_id, vector, model, dim, embedded_at)
                  VALUES (1, x'0000803f', 'nomic-embed-text', 1, '2026-04-23T00:00:00Z');
            ";
            cmd.ExecuteNonQuery();
        }
    }
}
