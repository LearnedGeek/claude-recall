On April 20 I was debugging a subtle issue with a Claude Code instance and [ANI, the AI companion I've been running as a research project](/Blog/Post/building-ani-ai-companion-for-grief) for the last eight months. We had a long back-and-forth and landed on a principle I phrased this way:

> *"Cosine similarity measures topical overlap. Parroting is verbatim phrase reuse. Those are different signals."*

We decided not to patch the issue at the reply layer. Treating the symptom, not the cause. Didn't want a guard that destroyed good replies to silence a bad one.

Three days later. Different Claude instance. Same bug resurfaces. I ask for a fix. Claude proposes — word for word, almost — the cosine-similarity guard I had rejected three days earlier. The prior reasoning existed on disk. Claude Code writes every session to `~/.claude/projects/<slug>/<uuid>.jsonl`, and the file holding my April 20 rejection was right there in that directory. But the active instance couldn't see it. It had been compacted out of context.

I said "search the history before we move on anything." Claude grepped. About forty seconds later we were both looking at my own April 20 quote again, verbatim. We moved on.

That's when the tool I'd been half-thinking about became something I had to actually build.

---

## The pattern had already been named

A few days earlier a Microsoft engineer published [I wasted 68 minutes a day re-explaining my code, then I built auto-memory](https://devblogs.microsoft.com/all-things-azure/i-wasted-68-minutes-a-day-re-explaining-my-code-then-i-built-auto-memory/). Copilot CLI, not Claude Code, but the same underlying shape. The agent is writing structured session data to disk already. The agent just isn't reading its own archive.

Different file format, same insight. Copilot CLI keeps a SQLite database. Claude Code writes line-per-turn JSONL. Either way, months of decisions are already persisted. The agent doesn't remember them because nothing's pointing it at them.

Credit to that post for crystallizing what I'd been feeling but hadn't named.

## What I built

`claude-recall` is three pieces:

1. **A small Python CLI that indexes the JSONL archive into SQLite with FTS5 full-text search.** Incremental, read-only, uses the bundled `sqlite3` module. Commands: `index`, `search`, `show`, `list`, `status`.

2. **Optional semantic rerank on top of the FTS5 results** via a local Ollama embedding model (`nomic-embed-text`). Turns *"find me the session where I said X"* into *"find me the session where I meant X."* FTS5 finds keyword overlap; embeddings catch the conceptual cousins.

3. **A `UserPromptSubmit` hook that fires on every message** I send in Claude Code and injects ranked prior-session matches as `additionalContext`. Latency scales with archive size — around 80 ms on small archives, climbing toward 1–2 seconds on 25,000-message corpora when the embed model has to be loaded. With Ollama warm and the binary doing the heavy lifting, the hook stays out of the way of how a prompt feels to send.

The hook is a NativeAOT-compiled binary now — `claude-recall-hook.exe` — not a Python wrapper. Every prompt used to pay a fresh Python interpreter tax and it was killing the UX. The binary fixed that in v0.4.

What this looks like in practice: when I drafted this paragraph, the hook ran. It searched 25,000 messages across 20 projects, ranked them against my current phrasing, and injected the top hits into this instance's context. I didn't do anything. I don't actually know what it found — Claude Code doesn't surface `additionalContext` in the UI — but the effect is visible: the instance stops making up prior decisions. It references them.

I also ran `claude-recall init-hooks` against claude-recall's own repo a few days ago. The tool is now in the loop on the sessions where I maintain it. That's a small thing to mention but it crossed a real threshold: the "agent reading its own prior work" pattern that motivated the project is now also how I work on the project.

## Three design principles

Short list, honestly stated:

- **Read-only against the archive.** Never modify what Claude Code writes. The archive is the source of truth; we consume it, we don't touch it.
- **Graceful degradation.** If the hook crashes, the prompt flows normally. A broken recall layer must never block the human.
- **Zero extra install hops in the common case.** FTS5 is already in every modern Python stdlib. The embedding layer is opt-in.

There's a deeper principle under those three: the tool's job is to make the archive cheap to query. It isn't trying to be clever about what to retrieve, or to summarize, or to editorialize. It finds things. The cleverness has to live in the agent you're already using, not in the recall layer.

## The honest caveats

This is v0.5.4, tagged beta. It works on my machine daily against a 25,000-message archive. It also has bugs — thirteen of them caught and closed in the dogfooding cycle, including one silent 27%-data-loss embedding bug that wouldn't have surfaced without real-world use. The v0.5 release landed on PyPI a few days ago, so the install story is now `pip install claude-recall` rather than a release-wheel URL.

A live example of what "actively maintained" looks like in this category: on the morning of April 25, the tool started failing in fresh installs. Claude Code v2.1.118, released two days earlier, had tightened its hook-schema validator and stopped accepting the legacy flat shape claude-recall had been generating since v0.4. The diagnosis came from running claude-recall against its own repo — which is also how the paper trail got captured — and v0.5.4 shipped the same morning, with a CHANGELOG entry citing the upstream release that triggered it. Memory tools live downstream of the agents they instrument, and the agents change. The healthy pattern is to assume that and build a tight loop, not to assume stability.

It isn't semantic-search-of-everything. It isn't a replacement for CLAUDE.md or for the `memory/` auto-memory system Claude Code ships natively. It isn't cross-machine — your session archive is local, the index stays local. That's a feature for me, since the archive has personal data in it; it may be a limitation if you need shared recall across teammates.

## If you want to try it

Public repo at [github.com/LearnedGeek/claude-recall](https://github.com/LearnedGeek/claude-recall), package on PyPI as `claude-recall`. MIT licensed. Install with `pip install 'claude-recall[embeddings]'` for the full thing, or skip the extras for FTS5-only. Beta quality — file issues when you break it, because you will, and I want to know.

What I'd specifically appreciate testing from early adopters:

- **Anything that crashes or returns silently empty.** The dogfooding cycle has hammered project auto-scoping, install-path edge cases, and a particularly nasty silent-data-loss bug in the embedding pipeline. The next class of issue is the one I haven't seen yet, and the only way to find it is to put the tool in front of someone whose archive isn't shaped like mine. Windows / macOS / Linux all welcome.

## The broader thing

Every agentic coding tool writes structured session data somewhere, and none of them have a native mechanism yet for the agent to read its own prior work. That gap is getting filled tool by tool. The Microsoft post is the Copilot CLI instance. `claude-recall` is the Claude Code instance. Whichever agent you use, the shape is the same: find the archive, index it cheaply, wire a hook, stop re-explaining.

The list is growing. As of the week this post goes up, there are at least three tools in the wild taking adjacent slices of this gap in genuinely different ways:

- **[claude-memory-mcp](https://pypi.org/project/claude-memory-mcp/)** takes the curated route — explicit `remember` / `forget` calls via MCP, knowledge graph with typed edges, a separate write-store. Best fit when you want to deliberately structure the facts the agent should hold onto.
- **[Beads](https://www.beads.dev)** treats it as task-tracking — Jira-shaped agent-readable state the agent consumes as part of doing work. Best fit when the durable artifact is what's *to do* rather than what's been *said*.
- **claude-recall** is read-only over what Claude Code already writes — passive hook injection, no curation, no separate store. Best fit when you want prior reasoning surfaced automatically without remembering to save it.

Three different theories of what "memory" means for an AI agent. Plenty of room for all three to coexist in someone's workflow.

It's also a small demonstration of something I keep running into across projects: when you deploy a system for long enough, the interesting problems show up in the seams. ANI taught me that [memory isn't just storage, it's an amplifier](/Blog/Post/park-et-al-generative-agents-memory). claude-recall is what happens when the same observation comes at me from the other direction — when the *agent I'm using* needs memory, not the agent I'm building.

---

*claude-recall is MIT-licensed and actively maintained. If you try it, I want to know what breaks. Related reading: [Why Your AI Assistant Keeps Guessing Wrong](/Blog/Post/ai-keeps-guessing-wrong) for the broader context pattern, or [ANI: The Architecture Behind the Companion](/Blog/Post/ani-the-architecture-behind-the-companion) for the companion project that triggered this one.*
