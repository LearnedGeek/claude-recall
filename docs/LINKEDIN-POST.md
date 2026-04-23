# LinkedIn Post — Draft

**Purpose:** Short-form announcement when claude-recall v0.2 ships.
**Audience:** Mark's LinkedIn network — developers, founders, technical folks who use AI coding tools.
**Tone:** Casual, direct, Mark's voice. Real pain → specific fix → link.
**Target length:** 150–250 words. Short enough to read in the feed, long enough to tell a story.

**Drafted:** April 23, 2026. Unpublished.

---

## Draft v1

I was losing an hour a day re-explaining decisions to Claude Code.

Every long session hits a context compaction eventually. The summary that survives is lossy. When I'd reference a decision from three days ago, Claude would make up a plausible answer that was subtly wrong, and I'd have to stop and correct it. Again.

The evidence was already on disk — Claude Code writes every session to a `.jsonl` file under `~/.claude/projects/`. But nothing queried those archives back into the active session. A Microsoft team had just shipped the same pattern for Copilot CLI (worth reading: [auto-memory post]). That crystallized it.

So I built claude-recall. A small Python CLI that indexes the Claude Code session archive into SQLite FTS5, plus two hooks — SessionStart primes each conversation with index status, UserPromptSubmit auto-queries the index when my prompt references prior work and injects matches as additional context. No manual search. No re-explaining.

Zero runtime deps. MIT license. Cross-platform (bash + PowerShell). `pip install claude-recall` (once v0.2 ships).

Repo: [link]
Technical write-up: [link to blog post]

If you're using Claude Code on anything with history, give it a try. Open issues, rip it apart. Happy to hear where it falls short.

---

## Draft v2 (shorter, more punch)

Claude Code writes every session to disk. Nothing queries those archives. So when I reference a decision from three days ago, Claude guesses — and guesses wrong.

Built claude-recall to fix it. SQLite FTS5 over the session archive, two hooks that inject ranked prior-session context automatically whenever I ask something that touches prior work.

No more pasting the same architecture explanation into Claude twice.

Zero runtime deps. MIT license. Cross-platform. `pip install claude-recall` when v0.2 lands.

Repo: [link]
Blog post with the full story: [link]

Inspired by the Microsoft auto-memory post for Copilot CLI — same pattern, different agent.

---

## Voice notes for Mark

**Keep:**
- First-person ("I was losing an hour a day")
- Specific number (one hour)
- Concrete pain (Claude guesses wrong, I have to correct)
- Honest attribution (Microsoft's post)
- Inviting tone for feedback ("open issues, rip it apart")

**Avoid:**
- "Excited to announce"
- "Proud to share"
- "Passion project"
- "Game-changer"
- Em-dashes — per the commit-style note in project memory, Mark prefers ASCII when writing for public consumption. Replace with colons, semicolons, or period breaks in the final version.
- Emoji

**Length:** Either draft works. v2 is tighter; v1 gives more context. Recommend v1 if the blog post is ready to link; v2 if announcing before the blog.

---

## Attachments / media for the post

- **Option A:** Screenshot of a claude-recall search output showing real results. High-trust; shows the tool works.
- **Option B:** The architecture ASCII diagram from README.md as a code-block inline. Low-overhead.
- **Option C:** No media — plain text performs fine on LinkedIn for technical content.

Lean: Option A when possible. Nothing sells a dev tool like a working screenshot.

---

## Hashtags (LinkedIn convention)

- #ClaudeCode
- #DeveloperTools
- #OpenSource
- #AI
- #Productivity

Keep it to 3–5. More looks spammy.

---

## Timing

Post when:
1. v0.2 is live on PyPI
2. Blog post is published
3. Repo is public with README polished

Not before all three. A LinkedIn post that links to a broken pip install or a missing blog damages credibility.

Optional: post-after-use. Let it run on your own projects for 2 weeks so the post includes a real "I used this for X and it caught Y" anecdote.

---

## Follow-up posts (if engagement is good)

If the first post gets traction, a second post a week later with a specific case study — *"claude-recall caught three things this week I'd have missed"* — converts better than a straight feature list.

Third post (if warranted): a technical deep-dive cross-posted from the blog, about the hook architecture specifically. Separate audience (folks building Claude Code extensions).

---

*End of LinkedIn draft.*
