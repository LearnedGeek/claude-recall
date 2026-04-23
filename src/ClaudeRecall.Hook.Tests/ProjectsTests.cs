using Microsoft.Data.Sqlite;
using Xunit;

namespace ClaudeRecall.Hook.Tests;

public class ProjectsTests
{
    [Fact]
    public void Slug_WindowsPath_LowercasesDriveLetter()
    {
        Assert.Equal(
            "e--Documents-Work-dev-repos-claude-recall",
            Projects.SlugFromPath("E:\\Documents\\Work\\dev\\repos\\claude-recall"));
    }

    [Fact]
    public void Slug_UnixPath_LeadingSlashBecomesDash()
    {
        Assert.Equal(
            "-Users-markm-dev-claude-recall",
            Projects.SlugFromPath("/Users/markm/dev/claude-recall"));
    }

    [Fact]
    public void Slug_PreservesNonDriveCase()
    {
        Assert.Equal(
            "e--Documents-Work-CamelCaseDir",
            Projects.SlugFromPath("E:\\Documents\\Work\\CamelCaseDir"));
    }

    [Fact]
    public void Slug_HandlesForwardSlashesOnWindows()
    {
        Assert.Equal(
            "e--Documents-Work-dev",
            Projects.SlugFromPath("E:/Documents/Work/dev"));
    }

    [Fact]
    public void Slug_EmptyPathReturnsEmpty()
    {
        Assert.Equal(string.Empty, Projects.SlugFromPath(string.Empty));
    }

    [Fact]
    public void Resolve_ReturnsStoredSlugCaseInsensitive()
    {
        using var conn = OpenInMemoryDb();
        SeedSession(conn, "E--Documents-Work-Legacy");

        var resolved = Projects.ResolveProjectSlug(conn, "E:\\Documents\\Work\\Legacy");

        // Canonical lowercase is what we derive; stored is the E-- form.
        // The resolver prefers the stored slug (case-preserving for SQL match).
        Assert.Equal("E--Documents-Work-Legacy", resolved);
    }

    [Fact]
    public void Resolve_FallsBackToCanonicalWhenNoMatch()
    {
        using var conn = OpenInMemoryDb();
        var resolved = Projects.ResolveProjectSlug(conn, "E:\\nonexistent\\path");
        Assert.Equal("e--nonexistent-path", resolved);
    }

    // ---- helpers ---------------------------------------------------------

    private static SqliteConnection OpenInMemoryDb()
    {
        SQLitePCL.Batteries_V2.Init();
        var conn = new SqliteConnection("Data Source=:memory:");
        conn.Open();
        using var cmd = conn.CreateCommand();
        cmd.CommandText = @"
            CREATE TABLE sessions (
              session_id TEXT PRIMARY KEY,
              project_slug TEXT NOT NULL,
              file_path TEXT NOT NULL UNIQUE,
              file_mtime REAL NOT NULL,
              started_at TEXT, ended_at TEXT,
              turn_count INTEGER NOT NULL DEFAULT 0,
              indexed_at TEXT NOT NULL);";
        cmd.ExecuteNonQuery();
        return conn;
    }

    private static void SeedSession(SqliteConnection conn, string projectSlug)
    {
        using var cmd = conn.CreateCommand();
        cmd.CommandText = @"
            INSERT INTO sessions(session_id, project_slug, file_path, file_mtime,
                                 turn_count, indexed_at)
            VALUES ('s1', $slug, '/tmp/x.jsonl', 1.0, 0, 'now')";
        cmd.Parameters.AddWithValue("$slug", projectSlug);
        cmd.ExecuteNonQuery();
    }
}
