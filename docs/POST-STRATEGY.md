# claude-recall — Post-Release Distribution Strategy

**Audience:** The project owner (Mark), and any future agent/session helping with distribution. This doc is the strategic frame; specific drafts (blog post, LinkedIn post, newsletter pitches, Discussion post) live in sibling docs or get produced on demand.

**Last updated:** 2026-04-24, post-v0.4.3 release.

---

## 1. Why this doc exists

After v0.4.3 shipped, the question came up: *"Is this tool valuable to others, and how do I distribute it?"* The answer is yes, it's valuable, and there's a realistic distribution path — but only if it's matched to the owner's actual preferences, not a generic founder playbook. This doc captures both the value assessment and a distribution plan that respects hard constraints.

---

## 2. Constraints (hard rules, do not work around)

The owner's stated preferences, verbatim, from the strategy conversation:

- **No Hacker News.** *"Too much noise and too 'social media' for me."*
- **No video production.** *"I'm NOT a video guy."*
- **No ongoing social-media presence.** *"I detest social media."*
- **LinkedIn is already a stretch.** Active, but conservatively — posts go out, ongoing engagement is not a standing commitment.
- **"Please do it for me" help is welcome** for drafting text, scripts, templates, PR descriptions. Not welcome: recording video, managing conversations after a post goes live.

These are preferences, not bike-sheddable. Any distribution idea that requires video production, HN engagement, or daily social-media presence is out of scope regardless of its theoretical leverage.

---

## 3. Honest value assessment

### What's genuinely good about claude-recall

- **Solves a real pain point.** Intra-session compaction is lossy; the raw `.jsonl` archive is sitting there unused. Anyone using Claude Code on a non-trivial project for more than a few weeks has hit *"I know I said something about X three weeks ago."*
- **Engineering discipline is visible.** Zero runtime deps for core. Graceful degradation throughout. 214 tests. Real dogfooding that surfaced and fixed 8 issues across 4 patch releases in ~24 hours.
- **Measured outcomes, not vibes.** 685ms → 80ms hook latency via the C# NativeAOT binary. 27% silent data loss caught and fixed, with the outcome quantified (72.2% → 99.95% coverage on a 25k-message archive).
- **Hook-system validation.** The project demonstrates Claude Code's extensibility model working end-to-end, which has value as a reference implementation beyond the tool itself.

### Honest caveats

- **Bus factor 1.** Single author, pre-1.0. Anthropic won't institutionally depend on it without a successor plan.
- **Windows x64 only for the fast path.** macOS/Linux users fall back to the slower Python hook until CI builds those platforms.
- **Ollama required for semantic.** Big install dep for non-ML users.
- **Anthropic could absorb the idea natively.** If Claude Code ships session-archive search in-product, claude-recall becomes an artifact rather than a going concern. Fine outcome, but worth knowing.

### Would Anthropic use it?

Realistic answer: they won't adopt external tooling into the product. But the project is exactly the shape that gets:
- Linked from official docs as a hook-system success story
- Name-checked in "what the community is building" roundups
- Referenced by the extensibility team as an example of what the hook system enables

Not guaranteed, but plausible — and a reasonable ceiling to aim for.

---

## 4. Distribution channels, ordered by leverage for this owner

Ordering principle: permanent homes + developer-native venues > performance venues. Zero ongoing presence required for channels 1-7.

### Tier 1 — mechanical, do first

**1. PyPI publish (the v0.5 milestone).**
The single biggest adoption unlock. `pip install claude-recall` vs. "paste this 120-character wheel URL" is night-and-day conversion. Permanent, indexable, confers legitimacy. Zero social energy.

- Requires: PyPI account + name registration + API token + one CI workflow addition.
- Blocks nothing. Do first.

**2. Blog post on learnedgeek.com.**
Draft already exists in `docs/BLOG-POST.md`. Owner's own site, owner's voice, indexable by search engines, permanent. People finding claude-recall in 2027 via *"claude code session search"* land here.

- Requires: review + polish of the existing draft + publish.
- The single asset that compounds over time.

**3. Cross-post blog to dev.to.**
Paste-once operation. Existing audience of developers. If it resonates, it surfaces in their daily email. No ongoing engagement expected.

- Requires: dev.to account + 5 minutes of copy-paste.

### Tier 2 — technical, non-performative

**4. GitHub Discussion in `anthropics/claude-code`.**
Technical venue, not performative. Framed as *"here's what I built on the hook system — sharing in case useful to the community."* Anthropic's own community lurks there; the extensibility team reads it. Highest single-venue chance of an Anthropic-adjacent boost.

- Requires: one post, no follow-up commitment beyond technical questions if they come.

**5. GitHub awesome-list PRs.**
Targets: `awesome-claude-code`, `awesome-rag`, `awesome-ollama`, `awesome-python` (tools section). Permanent entries; they become how future users discover the tool via "awesome list" searches and GitHub's own recommendation graph.

- Requires: a PR per list with a one-line description. Permanent once merged.

### Tier 3 — curated editorial channels

**6. Newsletter submissions.**
Editors skim submissions and include what fits. Zero ongoing presence. Targets in order of fit:
- **Python Weekly** — obvious fit for a Python tool
- **Simon Willison's newsletter / blog** — he covers Claude-adjacent tooling and picks up projects via email tip; high-quality-to-reach ratio is excellent
- **Latent Space** — if they're covering Claude Code / local LLM tooling that week
- **The Pragmatic Engineer** — reach is huge but fit is borderline; submit only if PyPI + blog post + GH Discussion are already live

Requires: one email per editor with a 15-second pitch. No follow-up unless they reply.

### Tier 4 — targeted developer-community posts

**7. r/ClaudeAI subreddit post.**
Smaller, warmer audience than HN. Technical developers who use Claude. One post, no presence required afterward. Comments usually stay on-topic.

- Requires: one post. If someone asks a technical question in comments, one reply is polite; no obligation beyond that.

### Tier 5 — accepted social channels

**8. LinkedIn post.**
Draft already exists in `docs/LINKEDIN-POST.md`. Owner is already doing LinkedIn; this fits. Weakest of the channels for actual adoption (LinkedIn readers aren't high-intent for CLI dev tools), but good for professional-network signal and credibility.

- Requires: review + publish. No ongoing comment engagement required.

### Explicitly NOT on this list

- **Hacker News** — owner's no. Do not suggest.
- **Twitter/X cold-posting** — social-media-scroll requirement; skip.
- **Video / YouTube** — owner's no.
- **Product Hunt** — wrong audience, wrong energy.
- **Conference talks** — scale is wrong at this stage, and it's performative.
- **Paid ads** — absurd for a free dev tool.

---

## 5. The video problem — alternatives that work

My original advice was *"record a 30-second demo first."* Owner doesn't do video. The alternatives, in order of preference:

**a) Annotated screenshots in the README and blog post.** 80% of the demo's value at 5% of the effort. Before / after the hook fires, highlighting the auto-injected `additionalContext`. I can write the captions; owner takes the screenshots.

**b) [asciinema](https://asciinema.org/) recording.** Terminal recorder — captures typed commands and output as a replayable text artifact. No face, no voice, no editing. Embeds cleanly in a blog post. If the owner decides they want a demo asset, this is the zero-production-energy path. I can script the exact command sequence to type.

**c) Skip demos entirely.** Text-with-screenshots is sufficient for a technical audience. Many widely-used dev tools (ripgrep, pre-commit, sqlite-utils) spread with no video asset.

Recommendation: **start with (a). Revisit (b) only if the written content is landing and a shareable quick-look would compound it.**

---

## 6. What the helper ("please do it for me") can draft

All of the following can be drafted by an agent for owner review and publish. Owner's responsibility shrinks to *review, edit lightly, click publish.*

- **Blog post revision pass** — polish the existing `docs/BLOG-POST.md` draft
- **LinkedIn post revision pass** — polish the existing `docs/LINKEDIN-POST.md` draft; variants for v0.5 relaunch
- **GitHub Discussion post** for `anthropics/claude-code`
- **Awesome-list PR descriptions** — one-liner entries + matching PR bodies for each list
- **Newsletter pitch emails** — one per editor, 15-second framing
- **r/ClaudeAI post** — intro paragraph + three-bullet "what's new" + link
- **asciinema script** — exact commands to type if owner chooses the terminal-demo route
- **PyPI-specific metadata polish** — classifiers, project URLs, long_description rendering check

What the helper cannot do:
- Press publish / post / send — owner's call, every time
- Register PyPI account, obtain API tokens, configure GitHub secrets
- Engage in follow-up comment threads after posts go live

---

## 7. Recommended sequencing

**Week 1 — mechanical foundation:**
- PyPI publish (v0.5.0 milestone). Nothing else gets easier until this is done.
- The helper drafts the v0.5 release notes + blog/LinkedIn polish while CI work is in-flight.

**Week 2 — writing + linking wave:**
- Blog post goes up on learnedgeek.com.
- Cross-post to dev.to.
- GitHub Discussion in `anthropics/claude-code`.
- Awesome-list PRs.
- LinkedIn post (owner already has a draft; publish when comfortable).

All five items above are ~15 minutes of owner-time each if the helper has drafted them.

**Week 3+ — curated editorial, once there's evidence of traction:**
- Newsletter submissions (Python Weekly first, then others if warranted).
- r/ClaudeAI post.

Watch for Anthropic or Simon Willison or another respected voice mentioning the project organically. If it happens, the channels above are already seeded to catch the resulting interest.

**Do not rush.** The tool's quality is its best distribution asset; a slow cadence of deliberate posts on permanent venues will compound. Fast spraying across channels dilutes attention.

---

## 8. Success criteria

Not metrics for their own sake, but signals worth watching:

- **PyPI weekly installs > 100** within a month of v0.5. Confirms basic discovery is working.
- **At least one inbound GitHub issue** from someone who is neither the owner nor known to the owner. Confirms non-dogfooder users exist.
- **A reference from Anthropic, Simon Willison, or a newsletter editor.** Any one of these is a major signal; don't chase them, but they validate the approach if they happen.
- **Zero social-media obligations acquired.** If the owner ends up feeling like they have to show up on Twitter or defend comments on HN, something has gone wrong with the plan — revisit this doc.

---

## 9. Failure modes to avoid

- **Spraying attention across too many channels at once.** The one-asset-at-a-time cadence matters; each channel feeds the next.
- **Expecting one post to go viral.** Dev tools spread slowly; six months from now is the real check-in.
- **Responding to social-media advice from people with different constraints.** This doc exists because the generic advice (video first, HN immediately, Twitter presence) doesn't fit. If someone suggests a new channel, check it against §2 Constraints before evaluating it.
- **Doing nothing because it feels performative.** The blog post is not performative — it's a technical artifact documenting what was built and what was learned. That distinction matters.
- **Believing LinkedIn is the best channel** because it's the most familiar. It's the weakest of the viable ones for actual tool adoption. Professional-network signaling, yes; adoption, not really.

---

## 10. Revisit triggers

Rewrite this document when:

- v0.5 ships and the PyPI tier-1 milestone is complete. Some channels will need updated copy.
- Anthropic ships native session-archive search in Claude Code. This changes the positioning from *"fills a gap"* to *"reference implementation."*
- A second maintainer joins. Bus-factor-1 caveat goes away; Anthropic adoption becomes more realistic.
- Real-world install count hits 1000+. Whole new distribution challenges (support, contribution handling, PR triage) — different doc.

---

**End of post-strategy.** Related: [BLOG-POST.md](BLOG-POST.md), [LINKEDIN-POST.md](LINKEDIN-POST.md), [INTEGRATION-GUIDE.md](INTEGRATION-GUIDE.md). For the tool itself: [PLAN.md](PLAN.md), [ARCHITECTURE.md](ARCHITECTURE.md), [EMBEDDINGS-PLAN.md](EMBEDDINGS-PLAN.md), [HOOK-BINARY-PLAN.md](HOOK-BINARY-PLAN.md).
