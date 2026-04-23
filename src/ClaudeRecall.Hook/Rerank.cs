// Rerank.cs — hybrid retrieval. Takes the top-N BM25 pool + the query
// string, optionally embeds and cosines to re-order the candidates.
//
// Mirrors _semantic_rerank in src/claude_recall/search.py.

using Microsoft.Data.Sqlite;

namespace ClaudeRecall.Hook;

internal sealed record RerankedResult(FtsResult Row, double BM25, double? Cosine);

internal static class Rerank
{
    /// <summary>
    /// Re-rank ``pool`` by cosine similarity vs. the embedded ``queryText``.
    /// Messages missing a vector row (or dim-mismatched) score -inf and sort
    /// to the bottom, preserving their BM25 rank relative to each other —
    /// matches Python behavior.
    /// </summary>
    public static (IReadOnlyList<RerankedResult> Results, string? FallbackReason) Run(
        SqliteConnection conn,
        OllamaClient client,
        string queryText,
        IReadOnlyList<FtsResult> pool)
    {
        if (pool.Count == 0)
        {
            return (Array.Empty<RerankedResult>(), null);
        }

        var ids = pool.Select(r => r.MsgId).ToArray();
        var vectorRows = Storage.LoadVectors(conn, ids);
        if (vectorRows.Count == 0)
        {
            return (PassthroughBM25(pool), "no vectors in index; run `claude-recall embed`");
        }

        float[] queryVec;
        try
        {
            queryVec = client.Embed(queryText);
        }
        catch (EmbeddingException ex)
        {
            return (PassthroughBM25(pool), $"Ollama embed failed: {ex.Message}");
        }

        // Compute cosine per row. Pool-position doubles as the original BM25 rank.
        var scored = new (int OrigIdx, double Score)[pool.Count];
        var queryDim = queryVec.Length;

        // Batch vectors we can actually cosine against (dim matches query).
        var eligibleIdx = new List<int>(pool.Count);
        var eligibleVecs = new List<float[]>(pool.Count);
        for (int i = 0; i < pool.Count; i++)
        {
            var id = pool[i].MsgId;
            if (vectorRows.TryGetValue(id, out var vr) && vr.Dim == queryDim)
            {
                eligibleIdx.Add(i);
                eligibleVecs.Add(Vectors.Unpack(vr.Vector, vr.Dim));
            }
        }

        // Initialize all scores to -inf (no vector → sort to bottom).
        for (int i = 0; i < scored.Length; i++)
        {
            scored[i] = (i, double.NegativeInfinity);
        }

        if (eligibleVecs.Count > 0)
        {
            var cosines = Vectors.CosineMatrix(queryVec, eligibleVecs);
            for (int k = 0; k < eligibleIdx.Count; k++)
            {
                scored[eligibleIdx[k]] = (eligibleIdx[k], cosines[k]);
            }
        }

        // Sort by cosine desc, stable by original BM25 rank.
        Array.Sort(scored, (a, b) =>
        {
            int cmp = b.Score.CompareTo(a.Score);
            return cmp != 0 ? cmp : a.OrigIdx.CompareTo(b.OrigIdx);
        });

        var reranked = new RerankedResult[scored.Length];
        for (int i = 0; i < scored.Length; i++)
        {
            var row = pool[scored[i].OrigIdx];
            reranked[i] = new RerankedResult(row, row.Score,
                double.IsNegativeInfinity(scored[i].Score) ? null : scored[i].Score);
        }
        return (reranked, null);
    }

    private static IReadOnlyList<RerankedResult> PassthroughBM25(IReadOnlyList<FtsResult> pool)
    {
        var list = new RerankedResult[pool.Count];
        for (int i = 0; i < pool.Count; i++)
        {
            list[i] = new RerankedResult(pool[i], pool[i].Score, null);
        }
        return list;
    }
}
