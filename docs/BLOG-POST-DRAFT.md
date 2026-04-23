# I re-proposed the exact fix my own session archive said no to. Then I checked the archive.

*Draft for learnedgeek.com. Mark-voice tuning still pending the author's rewrite pass; this is a starting surface, not a finished post.*

---

On April 20 I was debugging a subtle issue with a Claude Code instance and ANI, the AI companion I've been running as a research project for the last eight months. We had a long back-and-forth and landed on a principle I phrased this way:

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

3. **A `UserPromptSubmit` hook that fires on every message** I send in Claude Code and injects ranked prior-session matches as `additionalContext`. Latency on my machine: around 80 ms per prompt with rerank on. Below human perception.

The hook is a NativeAOT-compiled binary now — `claude-recall-hook.exe` — not a Python wrapper. Every prompt used to pay a fresh Python interpreter tax and it was killing the UX. The binary fixed that in v0.4.

What this looks like in practice: when I drafted this paragraph, the hook ran. It searched 25,000 messages across 20 projects, ranked them against my current phrasing, and injected the top hits into this instance's context. I didn't do anything. I don't actually know what it found — Claude Code doesn't surface `additionalContext` in the UI — but the effect is visible: the instance stops making up prior decisions. It references them.

## Three design principles

Short list, honestly stated:

- **Read-only against the archive.** Never modify what Claude Code writes. The archive is the source of truth; we consume it, we don't touch it.
- **Graceful degradation.** If the hook crashes, the prompt flows normally. A broken recall layer must never block the human.
- **Zero extra install hops in the common case.** FTS5 is already in every modern Python stdlib. The embedding layer is opt-in.

There's a deeper principle under those three: the tool's job is to make the archive cheap to query. It isn't trying to be clever about what to retrieve, or to summarize, or to editorialize. It finds things. The cleverness has to live in the agent you're already using, not in the recall layer.

## The honest caveats

This is v0.4, tagged beta. It works on my machine daily against a 25,000-message archive. It also has bugs — two install-path regressions caught and fixed in the last 48 hours, both closed before this post went up. The v0.5 release will land on PyPI so the install story becomes `pip install claude-recall` without a release-wheel URL.

It isn't semantic-search-of-everything. It isn't a replacement for CLAUDE.md or for the `memory/` auto-memory system Claude Code ships natively. It isn't cross-machine — your session archive is local, the index stays local. That's a feature for me, since the archive has personal data in it; it may be a limitation if you need shared recall across teammates.

## If you want to try it

Public repo at [github.com/LearnedGeek/claude-recall](https://github.com/LearnedGeek/claude-recall). MIT licensed. Install instructions in the README. Beta quality — file issues when you break it, because you will, and I want to know.

Two things I'd specifically appreciate testing from early adopters:

- **Multi-project installs.** Project auto-scoping landed in v0.2.1 but it only has me as its test case.
- **Install-path edge cases.** The v0.4.0 native-binary wheel had a packaging glob issue that bypassed CI and only surfaced when I upgraded my own install. There will be more of those. Windows, macOS, Linux all welcome.

## The broader thing

Every agentic coding tool writes structured session data somewhere, and none of them have a native mechanism yet for the agent to read its own prior work. That gap is getting filled tool by tool. The Microsoft post is the Copilot CLI instance. `claude-recall` is the Claude Code instance. Whichever agent you use, the shape is the same: find the archive, index it cheaply, wire a hook, stop re-explaining.

---

*claude-recall is MIT-licensed and actively maintained. If you try it, I want to know what breaks.*

---

## Notes for Mark's rewrite pass

**Voice drifts to watch for** — stuff that snuck in and is more my register than yours:

- "Below human perception" — phrase is fine but feels slightly more formal than your voice usually runs
- "The cleverness has to live in the agent you're already using" — idea is yours but the phrasing is mine; rewrite if it doesn't feel like you wrote it
- The section headers are clean but conventional; you tend to go a little more idiomatic when you want the reader to feel a post is casual

**Specific things you might want to add** that I deliberately left out:

- The Lerman / research-adjacent angle. I didn't pitch ANI because the post is about claude-recall, but you could close with one sentence about ANI being the stress-test case if it feels appropriate.
- Your consulting-context framing. Learned Geek Consulting readers may want to know where this fits in your broader work. I stayed out of that territory because I don't have your positioning down.

**Specific things to verify before publishing:**

- The 25k-message / 20-project number is accurate as of today — confirm before publishing in case your archive changes.
- The v0.4 latency number (~80 ms with rerank) came from the v0.4.0 release notes — re-confirm against v0.4.1 if you care to.
- v0.5 / PyPI timing — the post says "will land on PyPI" but doesn't commit to a date. Good — keeps you flexible.
- The Apr 20 quote attribution. In the post I just call it "a principle I phrased this way" which is accurate. You may want to add or remove framing around that depending on how much context you want to give readers about the mark-LLM meta-pattern.
