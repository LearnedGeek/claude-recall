# Blog Post — Draft Outline

**Purpose:** Launch post for claude-recall on learnedgeek.com, cross-posted to dev.to.
**Audience:** Claude Code power users. Developers who've felt the pain of context compaction. Not a marketing audience — a peer audience.
**Target length:** 800–1200 words.
**Tone:** Casual. First-person. Specific anchors. Humor beats. No Latinate bloat. This is a Mark voice draft — he'll rewrite for final, but start closer to his register than my default.

**Drafted:** April 23, 2026. Unpublished.

---

## Working title (pick one)

- *"I kept re-explaining my project to Claude Code until I built claude-recall."*
- *"Claude Code has a memory problem. I built the query layer it was missing."*
- *"Your Claude sessions are already on disk. Here's how to actually use them."*

Lean: option 1. Personal, specific, and the pattern (X happened until I built Y) sets up the reader.

---

## Outline

### Hook (150 words)

Open with the real moment. Mid-session, context compacts, summary is lossy, I ask Claude about something we'd decided three days ago, and Claude makes up a plausible answer that's subtly wrong. I catch it. Thirty minutes later it happens again. I realize I've been losing an hour a day to this.

Reference the Microsoft auto-memory post — credit where due, same pattern, different agent. That post crystallized what I'd been feeling but hadn't named.

### The specific trigger (200 words)

Pick one specific, real incident to anchor the reader. E.g. the April 21 conversation about regex patterns — Claude suggested a regex-based solution, I had to stop and say *"we decided against regex for this exact reason three weeks ago."* I had to go grep the raw session log to prove it. The evidence existed on disk. Claude couldn't see it.

Be honest about the fix attempt that didn't work — adding a feedback memory ("next time I mention a prior decision, grep the archive") helped but wasn't reliable. Neither Claude nor I remembered to invoke it consistently. The behavior rule needed mechanism.

### The realization (150 words)

Claude Code writes every session to a `.jsonl` file at `~/.claude/projects/<project>/<session>.jsonl`. I'd seen the path before — the compaction message at the top of sessions literally links to the file. But the archive was just there, sitting, unqueried.

The Microsoft post pattern: *the agent already writes structured session data. Add a cheap query layer. Make it automatic.* That applies here exactly. Different file format, same insight.

### What I built (250 words)

Three pieces. Be specific.

1. **A small Python CLI** — indexes the `.jsonl` archive into SQLite with FTS5. No external deps. ~250 lines. Commands: `index`, `search`, `show`, `list`, `status`.
2. **Two Claude Code hooks** — `SessionStart` primes the conversation with a one-line index status; `UserPromptSubmit` auto-queries the index whenever I ask something and injects ranked matches as `additionalContext`.
3. **`init-hooks` command** — drops the hooks into any project's `.claude/settings.json` in one command. Cross-platform (bash + PowerShell).

Show the shape. One short code block of the `settings.json` that gets merged in. One `claude-recall search` example with a real result. Don't dump the whole CLI spec — point at the docs.

### Why this design (150 words)

Briefly, the principles:

- **Read-only against the archive.** Don't mess with what Claude Code writes.
- **Zero runtime deps.** `pip install claude-recall` should be instant.
- **Graceful degradation.** A hook crash must never block a Claude Code prompt.
- **Two hooks, not one.** SessionStart primes; UserPromptSubmit queries with the prompt as the query. Prompt-scoped injection is high-precision.

The SQLite FTS5 choice — why not a vector DB, why not grep, why not a third-party tool — link to ARCHITECTURE.md for depth.

### Real result (100 words)

The concrete outcome on my active project (ANI Runtime). Name a specific thing that hook recall caught and saved me from re-explaining. Something like: *"Two days after wiring it in, I asked Claude to `take the approach from the OG Ani conversation about [X]` and Claude's answer came back referencing the actual exchange, timestamps included. I didn't paste anything. The hook had already done it."*

Name the token savings too. Estimate: 600-token recall injection vs. 16,000-token grep I would have done manually if I'd remembered. Matches the auto-memory article's 200× claim order.

### Limits and honest caveats (100 words)

Things it isn't, so the reader calibrates:

- Not semantic search (FTS5 only in MVP; embeddings opt-in in v0.3).
- Not a replacement for CLAUDE.md-style curated memory.
- Not multi-machine (local only).
- MVP. Needs real-world wear-testing.

### Close + call to action (100 words)

Link to the GitHub repo. Mention license (MIT). Explicit invitation to try it on a project and open issues.

Credit the auto-memory post again as the direct inspiration.

One line about context across tools — how Copilot CLI, Claude Code, Cursor all write structured session data now; the query layer pattern applies to all of them; this is the Claude Code instance.

---

## Key phrases to keep in Mark's voice (lean casual, not marketing)

- "It just sat there, unqueried."
- "I'd been losing an hour a day to this."
- "Claude couldn't see it."
- "The evidence existed on disk."
- "The behavior rule needed mechanism."
- "Pointed at the docs, not the whole spec."

## Phrases to avoid (my default register that Mark will cut)

- "Leverage" (any usage)
- "Empower", "unlock", "elevate"
- "Robust", "scalable", "production-ready"
- "Seamlessly", "effortlessly"
- "At its core"
- Any sentence beginning with "In today's fast-paced..."

## Call-to-action candidates

1. "If you're on Claude Code and your project has any history to speak of, try it. Break it, file issues."
2. "MIT licensed. `pip install claude-recall` (once v0.2 ships). Repo at [link]."

---

## Publication checklist (for when final draft is ready)

- [ ] Rewritten by Mark in his voice (this is an outline, not final copy)
- [ ] Real anchor incident selected and verified against the session archive
- [ ] Token-savings claim backed by an actual measurement, not a hand-wave
- [ ] Code samples tested — the `settings.json` merge actually works as shown
- [ ] GitHub repo public, v0.1.0 tag in place, README polished
- [ ] Microsoft auto-memory post linked and credited
- [ ] Cross-post plan: learnedgeek.com primary, dev.to mirror, Hacker News Show HN optional

---

*End of blog post outline.*
