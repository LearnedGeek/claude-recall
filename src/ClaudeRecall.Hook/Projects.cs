// Projects.cs — port of src/claude_recall/projects.py.
//
// Derives the Claude Code session-archive slug from a working directory.
// Must produce byte-identical output to the Python implementation so the
// binary and Python CLI resolve the same project_slug for the same cwd.

using Microsoft.Data.Sqlite;

namespace ClaudeRecall.Hook;

internal static class Projects
{
    /// <summary>
    /// Derive the Claude Code project slug from an absolute directory path.
    /// Windows: "E:\\Documents\\Work" → "e--Documents-Work"
    /// Unix:    "/Users/m/dev"       → "-Users-m-dev"
    /// Pure function — no filesystem access, no DB.
    /// </summary>
    public static string SlugFromPath(string path)
    {
        if (string.IsNullOrEmpty(path))
        {
            return string.Empty;
        }

        // Lowercase the Windows drive letter only (preserve rest of the case).
        if (path.Length >= 2 && path[1] == ':' && char.IsLetter(path[0]))
        {
            path = char.ToLowerInvariant(path[0]) + path[1..];
        }

        // Replace ':' then '\' then '/' — order matches the Python sequence.
        var chars = path.ToCharArray();
        for (int i = 0; i < chars.Length; i++)
        {
            if (chars[i] == ':' || chars[i] == '\\' || chars[i] == '/')
            {
                chars[i] = '-';
            }
        }
        return new string(chars);
    }

    /// <summary>
    /// Resolve the project_slug stored in the index that corresponds to ``cwd``.
    /// Returns the exact stored slug (case-preserved) when a case-insensitive
    /// match exists, else the canonical lowercase derivation, else null.
    /// Mirrors ``projects.resolve_project_slug`` in Python.
    /// </summary>
    public static string? ResolveProjectSlug(SqliteConnection conn, string? cwd)
    {
        cwd ??= Environment.CurrentDirectory;
        var canonical = SlugFromPath(Path.GetFullPath(cwd));
        if (string.IsNullOrEmpty(canonical))
        {
            return null;
        }

        using var cmd = conn.CreateCommand();
        cmd.CommandText = "SELECT project_slug FROM sessions " +
                          "WHERE LOWER(project_slug) = LOWER($s) LIMIT 1";
        cmd.Parameters.AddWithValue("$s", canonical);
        var result = cmd.ExecuteScalar();
        return result is string s ? s : canonical;
    }
}
