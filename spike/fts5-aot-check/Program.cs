// Spike: verify Microsoft.Data.Sqlite + FTS5 bm25() + snippet() work under
// NativeAOT publish, and measure cold-start latency. If this binary boots
// under ~100ms and returns correct FTS5 results, the v0.4 hook architecture
// is viable.
//
// Usage:
//   fts-aot-check <path-to-claude-recall-index.db> <query>

using System.Diagnostics;
using Microsoft.Data.Sqlite;

var startupStopwatch = Stopwatch.StartNew();

if (args.Length < 2)
{
    Console.Error.WriteLine("usage: fts-aot-check <db-path> <query>");
    return 1;
}

var dbPath = args[0];
var query = args[1];

if (!File.Exists(dbPath))
{
    Console.Error.WriteLine($"db file not found: {dbPath}");
    return 2;
}

// Open read-only; we're only querying.
var connStr = new SqliteConnectionStringBuilder
{
    DataSource = dbPath,
    Mode = SqliteOpenMode.ReadOnly,
    Cache = SqliteCacheMode.Private,
}.ToString();

using var conn = new SqliteConnection(connStr);
conn.Open();

var openedAt = startupStopwatch.ElapsedMilliseconds;

// Query FTS5 with BM25 + snippet. Same shape as the Python search.py.
const string sql = """
    SELECT
      s.session_id,
      m.turn_index,
      m.role,
      snippet(messages_fts, 0, '<mark>', '</mark>', '...', 24) AS snippet,
      -bm25(messages_fts) AS score
    FROM messages_fts
    JOIN messages m ON m.msg_id = messages_fts.rowid
    JOIN sessions s ON s.session_id = m.session_id
    WHERE messages_fts MATCH $q
    ORDER BY bm25(messages_fts) ASC
    LIMIT 5
    """;

using var cmd = conn.CreateCommand();
cmd.CommandText = sql;
cmd.Parameters.AddWithValue("$q", query);

int rowCount = 0;
using (var reader = cmd.ExecuteReader())
{
    while (reader.Read())
    {
        rowCount++;
        var sid = reader.GetString(0);
        var turn = reader.GetInt32(1);
        var role = reader.GetString(2);
        var snippet = reader.GetString(3);
        var score = reader.GetDouble(4);
        Console.WriteLine($"[{sid[..Math.Min(8, sid.Length)]} turn {turn} {role}] " +
                          $"score={score:F3}  {snippet}");
    }
}

var queriedAt = startupStopwatch.ElapsedMilliseconds;
Console.WriteLine();
Console.WriteLine($"-- matched {rowCount} rows");
Console.WriteLine($"-- timings: open={openedAt}ms, total={queriedAt}ms");
return 0;
