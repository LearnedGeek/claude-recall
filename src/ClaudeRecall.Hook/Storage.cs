// Storage.cs — SQLite FTS5 reader. Mirrors the SELECT in src/claude_recall/search.py.
//
// Read-only access to the index.db. Same FTS5 MATCH + bm25() + snippet()
// shape as the Python side. Vector lookup path added for semantic rerank.

using Microsoft.Data.Sqlite;

namespace ClaudeRecall.Hook;

internal sealed record FtsResult(
    long MsgId,
    string SessionId,
    string ProjectSlug,
    string? StartedAt,
    int TurnIndex,
    string Role,
    string Content,
    string Snippet,
    double Score
);

internal sealed record VectorRow(long MsgId, byte[] Vector, int Dim);

internal static class Storage
{
    // FTS5 snippet parameters — byte-identical to Python search.py constants.
    public const string SnippetStart = "<mark>";
    public const string SnippetEnd = "</mark>";
    public const string SnippetTrunc = "...";
    public const int SnippetTokens = 24;

    public static SqliteConnection OpenReadOnly(string dbPath)
    {
        SQLitePCL.Batteries_V2.Init();
        var cs = new SqliteConnectionStringBuilder
        {
            DataSource = dbPath,
            Mode = SqliteOpenMode.ReadOnly,
            Cache = SqliteCacheMode.Private,
        }.ToString();
        var conn = new SqliteConnection(cs);
        conn.Open();
        return conn;
    }

    /// <summary>
    /// Run an FTS5 MATCH with BM25 ranking, optional project scope, optional
    /// days cutoff, bounded by ``poolSize``. Results ordered by BM25 asc
    /// (most-relevant first), score field negated so higher = better for
    /// user-facing display.
    /// </summary>
    public static List<FtsResult> SearchFts5(
        SqliteConnection conn,
        string ftsQuery,
        string? projectSlug,
        int days,
        int poolSize)
    {
        var cutoffIso = DateTime.UtcNow.AddDays(-days)
            .ToString("yyyy-MM-ddTHH:mm:ss.fffffffK");

        var snippet = $"snippet(messages_fts, 0, '{SnippetStart}', '{SnippetEnd}', " +
                      $"'{SnippetTrunc}', {SnippetTokens})";

        string sql =
            $@"SELECT
                m.msg_id,
                s.session_id,
                s.project_slug,
                s.started_at,
                m.turn_index,
                m.role,
                m.content,
                {snippet} AS snippet,
                -bm25(messages_fts) AS score
              FROM messages_fts
              JOIN messages m ON m.msg_id = messages_fts.rowid
              JOIN sessions s ON s.session_id = m.session_id
              WHERE messages_fts MATCH $q
                AND (s.started_at IS NULL OR s.started_at >= $cutoff)";
        if (!string.IsNullOrEmpty(projectSlug))
        {
            sql += " AND s.project_slug = $slug";
        }
        sql += $" ORDER BY bm25(messages_fts) ASC LIMIT {poolSize}";

        using var cmd = conn.CreateCommand();
        cmd.CommandText = sql;
        cmd.Parameters.AddWithValue("$q", ftsQuery);
        cmd.Parameters.AddWithValue("$cutoff", cutoffIso);
        if (!string.IsNullOrEmpty(projectSlug))
        {
            cmd.Parameters.AddWithValue("$slug", projectSlug);
        }

        var results = new List<FtsResult>();
        using var reader = cmd.ExecuteReader();
        while (reader.Read())
        {
            results.Add(new FtsResult(
                MsgId: reader.GetInt64(0),
                SessionId: reader.GetString(1),
                ProjectSlug: reader.GetString(2),
                StartedAt: reader.IsDBNull(3) ? null : reader.GetString(3),
                TurnIndex: reader.GetInt32(4),
                Role: reader.GetString(5),
                Content: reader.GetString(6),
                Snippet: reader.GetString(7),
                Score: reader.GetDouble(8)));
        }
        return results;
    }

    /// <summary>Runs the same search but with the OR-joined sanitized fallback on zero rows.</summary>
    public static List<FtsResult> SearchFts5WithFallback(
        SqliteConnection conn,
        string rawQuery,
        string sanitizedQuery,
        string? projectSlug,
        int days,
        int poolSize)
    {
        // Try raw first; on parse error or zero results, retry with sanitized.
        List<FtsResult> rows;
        try
        {
            rows = SearchFts5(conn, rawQuery, projectSlug, days, poolSize);
        }
        catch (SqliteException)
        {
            rows = new List<FtsResult>();
        }
        if (rows.Count > 0 || string.IsNullOrEmpty(sanitizedQuery) || sanitizedQuery == rawQuery)
        {
            return rows;
        }
        try
        {
            return SearchFts5(conn, sanitizedQuery, projectSlug, days, poolSize);
        }
        catch (SqliteException)
        {
            return rows; // stick with the empty raw result rather than throw
        }
    }

    /// <summary>Load vector rows for a set of msg_ids. Missing rows are simply absent.</summary>
    public static Dictionary<long, VectorRow> LoadVectors(
        SqliteConnection conn,
        IReadOnlyList<long> msgIds)
    {
        var result = new Dictionary<long, VectorRow>();
        if (msgIds.Count == 0)
        {
            return result;
        }

        // Inline the IDs to keep this simple; msgIds is bounded by rerank_pool_size (50 default).
        var placeholders = string.Join(",", Enumerable.Range(0, msgIds.Count).Select(i => $"$p{i}"));
        using var cmd = conn.CreateCommand();
        cmd.CommandText =
            $"SELECT msg_id, vector, dim FROM message_vectors WHERE msg_id IN ({placeholders})";
        for (int i = 0; i < msgIds.Count; i++)
        {
            cmd.Parameters.AddWithValue($"$p{i}", msgIds[i]);
        }

        using var reader = cmd.ExecuteReader();
        while (reader.Read())
        {
            var id = reader.GetInt64(0);
            var blob = (byte[])reader.GetValue(1);
            var dim = reader.GetInt32(2);
            result[id] = new VectorRow(id, blob, dim);
        }
        return result;
    }
}
