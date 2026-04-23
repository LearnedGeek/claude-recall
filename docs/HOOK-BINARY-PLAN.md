# claude-recall — v0.4 C# Hook Binary Plan

**Audience:** The agent or engineer implementing v0.4.
**Status:** Design complete. Scoping approved 2026-04-23. AOT feasibility spike green. Implementation has not started.
**Relation to prior docs:**
- [docs/PLAN.md](PLAN.md) — overall product spec, still authoritative.
- [docs/EMBEDDINGS-PLAN.md](EMBEDDINGS-PLAN.md) — v0.3 feature, shipped.
- This doc scopes the v0.4 feature: a NativeAOT-compiled C# hook binary that replaces the Python-based `UserPromptSubmit` hook path to eliminate the 500ms budget regression introduced by semantic rerank.

---

## 1. Intent

v0.3.0 shipped the semantic retrieval layer with `[embeddings].use_in_hook = false` default because real measurement showed the Python hook at ~685ms with semantic on — over the 500ms budget from PLAN §7.3. The bottleneck is per-invocation Python + numpy + httpx cold-import cost (~350ms combined), not anything about the retrieval logic itself.

**v0.4 replaces the hook layer with a NativeAOT-compiled C# single-file executable** that does keyword extraction + FTS5 BM25 retrieval + Ollama embedding + cosine rerank + agent-context formatting, in under 100ms cold-start. Python CLI remains the source of truth for index/embed/search/status; the binary is a hot-path specialist.

Spike results (2026-04-23, verified on this host):

| Operation | C# AOT binary | Python CLI |
|---|---|---|
| Cold start + empty program | ~13ms | ~110ms |
| Cold start + FTS5 MATCH + bm25 + snippet | 16ms (avg of 10) | 125ms |
| Projected with Ollama embed + cosine rerank | **~65-80ms** | 685ms |

---

## 2. Non-goals

- **Not a full Python-CLI replacement.** `claude-recall index`, `embed`, `search`, `show`, `list`, `status`, `init-hooks` all stay in Python. Users invoke these directly, and the indexing side has no latency budget.
- **Not a second data model.** The binary reads the SAME SQLite file, the SAME `config.toml`, the SAME `message_vectors` BLOBs. One source of truth. If the binary and Python disagree on output, the binary is wrong.
- **Not a network server, daemon, or service.** One-shot subprocess per invocation, same shape as the current shell hooks.
- **Not cross-platform in v0.4.** Windows x64 only. macOS and Linux land post-v0.4 when we have users and concrete demand.
- **Not a rewrite of anything that currently works.** Python indexer, embed pipeline, keyword extraction, semantic search logic — all stay. The binary ports a narrow slice (hook search path) to C#.
- **Not a new language ecosystem in the repo.** Tomlyn + Microsoft.Data.Sqlite + System.Text.Json. No other C# dependencies. Matching the Python side's "stdlib-first" discipline.

---

## 3. Architecture

```
┌──────────────────────────────────────────────────────────┐
│  Claude Code session (user types a prompt)                │
└──────────────────────┬───────────────────────────────────┘
                       │ settings.json → UserPromptSubmit hook
                       │ spawns process with stdin JSON
                       ▼
┌──────────────────────────────────────────────────────────┐
│  claude-recall-hook.exe  (NativeAOT single-file, ~2.3MB)  │
│                                                           │
│  1. Read stdin: {"prompt": "..."}                         │
│  2. Load config.toml from XDG/APPDATA (Tomlyn)           │
│  3. Resolve cwd → project_slug (slug_from_path logic)    │
│  4. Extract keywords (stdlib-style stopword strip)        │
│  5. Open index.db read-only (Microsoft.Data.Sqlite)      │
│  6. FTS5 MATCH + bm25() + snippet(), LIMIT 50             │
│  7. If embeddings enabled AND use_in_hook:                │
│     a. POST /api/embed to Ollama (HttpClient)            │
│     b. Load vectors for pool, cosine-rank (SIMD hint)    │
│  8. Write stdout: {"additionalContext": "..."} or {}     │
│  9. Exit 0 always (hook failsafe)                         │
└──────────────────────────────────────────────────────────┘
                       │
                       ▼
                 Claude Code merges additionalContext
                 into the active prompt context.
```

The binary is behaviorally identical to `claude-recall search --project auto --from-config --extract-keywords --semantic-from-config --agent-context` — just compiled. Python tests that validate the shell-hook JSON contract transfer directly.

---

## 4. Tech stack

| Component | Choice | Rationale |
|---|---|---|
| Language | C# 12 / .NET 9 | Your preference; NativeAOT is production-ready in .NET 8+ |
| Packaging | NativeAOT single-file publish | ~16ms cold start (verified). 4MB artifact. Zero runtime install on the user's machine. |
| SQLite | `Microsoft.Data.Sqlite` 9.0.x + `SQLitePCLRaw.bundle_e_sqlite3` 2.1.x | Bundled SQLite with FTS5 enabled. AOT-clean. Verified by spike. |
| TOML | `Tomlyn` | Mature, AOT-safe, handles the four sections claude-recall uses. |
| HTTP | `System.Net.Http.HttpClient` (stdlib) | Zero dep. |
| JSON | `System.Text.Json` with source-generated contexts | AOT-required for deserialization. |
| Numeric | `System.Numerics.Vectors` for cosine | SIMD where available; trivial fallback. |
| Build | `dotnet publish -c Release -r win-x64 --self-contained /p:PublishAot=true` | Single command. |

External runtime deps on the user's machine: **zero**. The 2.3MB binary statically links everything.

Build-time deps on a contributor's machine: **.NET 9 SDK + MSVC C++ Build Tools** (for the CRT static libs). Contributors fixing only Python don't need these.

---

## 5. Data-model boundary with Python

The binary and the Python CLI share three files:

1. **`index.db`** — SQLite file at `cfg.database.path`. Schema owned by `storage.py`. Binary opens **read-only** (`SqliteOpenMode.ReadOnly`) and never writes. Any schema change stays a Python-side responsibility; the binary fails gracefully on an unexpected schema version.
2. **`config.toml`** — read by both sides. Same XDG (`$XDG_CONFIG_HOME/claude-recall/config.toml`) / APPDATA (`%APPDATA%\claude-recall\config.toml`) resolution.
3. **`message_vectors.vector` BLOBs** — little-endian float32 per `embeddings.py::pack_vector`. Binary reads `System.Buffers.Binary.BinaryPrimitives.ReadSingleLittleEndian` (or a `Span<byte>` → `ReadOnlySpan<float>` reinterpret).

**Versioning:** both sides include `schema_version = 1` as a constant. If the binary sees a higher version, it logs one line to stderr and emits `{}` — the hook failsafe. No attempt to forward-migrate.

**Slug derivation:** the binary implements `slug_from_path` with identical rules to `projects.py::slug_from_path` (lowercase drive letter, separators → `-`). Tested against Python fixtures.

**Vector dimension:** read from `message_vectors.dim`. Rows where `dim != query_vec.Length` are dropped with `-inf` score (identical to Python behavior).

---

## 6. Binary CLI surface

```
claude-recall-hook [--config <path>] [--version] [--probe]
                   [--no-semantic] [--verbose] [--timeout-ms N]
```

Invoked without args: reads `{"prompt": "..."}` from stdin, does the hook work, writes JSON to stdout. No positional args — the hook protocol is stdin/stdout, not argv.

**Flags:**
- `--config <path>` — override config.toml location (mirrors Python CLI `--config`)
- `--version` — print `claude-recall-hook 0.4.0` and exit 0
- `--probe` — instead of reading stdin, run Ollama reachability + model + embed probe and exit 0/2. Mirrors `claude-recall embed --probe`.
- `--no-semantic` — force FTS5-only for debugging, regardless of config
- `--verbose` — print timing breakdown to stderr (for perf investigations)
- `--timeout-ms N` — override Ollama HTTP timeout (default from config.request_timeout_seconds)

**Exit codes:** always 0 when invoked as a hook. Non-zero only for `--probe` and `--version` (exit 0) / `--probe` failure (exit 2). Hook failsafe: ANY unhandled exception → catch all → write `{}` to stdout → exit 0.

**Output contract:** identical to v0.3 `claude-recall search --agent-context` — either `{}` or `{"additionalContext": "<string>"}`. Validated by Python tests subprocess-invoking the binary.

---

## 7. Init-hooks integration

`claude-recall init-hooks` gains a new responsibility: prefer the binary over the shell script on supported platforms.

**Decision tree at init-hooks time:**
```
if package_data contains claude-recall-hook.exe (Windows)
   or claude-recall-hook (Unix when we add it):
       copy the binary into .claude/hooks/
       write .claude/hooks/.claude-recall-version with pkg version
       write settings.json to reference the binary directly
else:
       fall back to the v0.3 behavior (.ps1 / .sh wrapping Python CLI)
```

The binary is registered directly as the `command` field — no shell wrapper needed since the binary reads stdin + writes stdout natively:

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {"command": ".claude/hooks/claude-recall-hook.exe"}
    ]
  }
}
```

SessionStart hook stays as the PowerShell/bash wrapper for now — it's not latency-critical and calls `claude-recall status` once per session.

**Config flag flip:** with the binary in the hook path, `[embeddings].use_in_hook` default flips to `true` in v0.4.0 since the budget constraint is gone. Users who don't want semantic can still flip it off.

---

## 8. Distribution strategy

**Wheel-per-platform via GitHub Actions.**

```yaml
# .github/workflows/build-wheels.yml (excerpt)
jobs:
  build-wheel-win-x64:
    runs-on: windows-latest    # MSVC C++ build tools pre-installed
    steps:
      - actions/checkout
      - setup-dotnet 9.0.x
      - setup-python 3.12
      - run: dotnet publish src/ClaudeRecall.Hook -c Release -r win-x64 --self-contained
      - run: copy the published exe + e_sqlite3.dll to src/claude_recall/native/
      - run: python -m build --wheel
      - upload-artifact: dist/claude_recall-0.4.0-cp3-none-win_amd64.whl

  build-wheel-pure-python:
    # Fallback wheel with no binary — for platforms we haven't built yet.
    # init-hooks on this wheel falls back to the Python-CLI shell hook.
    runs-on: ubuntu-latest
    steps:
      - actions/checkout
      - run: python -m build --wheel -- -C --plat-name=any
      - upload-artifact: dist/claude_recall-0.4.0-py3-none-any.whl
```

Users on Windows x64 get the binary; users on macOS/Linux get the pure-Python fallback. Same `pip install` command. Both install cleanly; init-hooks adapts.

**Platform tag strategy:**
- `claude_recall-0.4.0-cp3-none-win_amd64.whl` — wheel with `src/claude_recall/native/claude-recall-hook.exe` bundled
- `claude_recall-0.4.0-py3-none-any.whl` — pure-Python wheel, no binary, Python-hook fallback

When v0.4.1 adds osx-arm64 and linux-x64 binaries, the matrix expands. No change to the design.

**PyPI publish** — gated on v0.2 milestone (PLAN §13) but this release shape unblocks it. First PyPI publish could be v0.4.0 or v0.5.0 depending on appetite.

---

## 9. Module layout

```
claude-recall/
├── src/
│   ├── claude_recall/                  # existing Python package
│   │   ├── ...existing...
│   │   └── native/                     # NEW — package data for the binary
│   │       └── claude-recall-hook.exe  # populated by build, gitignored
│   │
│   └── ClaudeRecall.Hook/              # NEW — C# project (solution folder)
│       ├── ClaudeRecall.Hook.csproj
│       ├── Program.cs
│       ├── Config.cs                   # Tomlyn wrapper
│       ├── Projects.cs                 # slug_from_path
│       ├── Keywords.cs                 # stopword extraction
│       ├── Storage.cs                  # SQLite + FTS5
│       ├── Embeddings.cs               # HttpClient + cosine + BLOB codec
│       ├── Rerank.cs                   # hybrid retrieval
│       └── JsonContext.cs              # System.Text.Json source-gen
│
├── spike/                              # existing, moved out of root earlier
│   └── fts5-aot-check/
│
├── .github/workflows/
│   ├── test.yml                        # existing
│   └── build-wheels.yml                # NEW
│
├── build-hook.ps1                      # NEW — one-shot build for local dev
├── CONTRIBUTING.md                     # NEW — MSVC setup notes for C# contributors
└── pyproject.toml                      # existing, minor additions for package_data
```

`pyproject.toml` additions:
```toml
[tool.setuptools.package-data]
"claude_recall" = ["native/*"]
```

Single repo, two build systems. `dotnet build` does the C# side; `pip install -e .` does the Python side. `build-hook.ps1` runs both in sequence for local dev.

---

## 10. Implementation order

Each step is independently testable; previous step's tests must pass before proceeding.

1. **Wire up the C# project skeleton.** Move/create `src/ClaudeRecall.Hook/ClaudeRecall.Hook.csproj`. Verify `dotnet publish -r win-x64 --self-contained /p:PublishAot=true` produces a runnable exe. No logic yet — just an AOT-publishable hello-world.
2. **Port `projects.py::slug_from_path` to `Projects.cs`.** Pure function, easy to unit-test against the 7 Python test cases.
3. **Port `keywords.py` to `Keywords.cs`.** Embed the stopword list as a `FrozenSet<string>`. Regex for tokens. Unit-tested against all 13 Python cases.
4. **Config reader (`Config.cs`).** Tomlyn-based. Reads only the fields the hook needs (`[archive]`, `[database]`, `[search]`, `[embeddings]`). Validates XDG/APPDATA path resolution.
5. **SQLite + FTS5 (`Storage.cs`).** FTS5 MATCH + bm25 + snippet + pool LIMIT. Spike code becomes the basis. Unit-tested against fixtures from `tests/fixtures/`.
6. **Embeddings (`Embeddings.cs`).** HttpClient to Ollama. BLOB → `float[]` via `BinaryPrimitives`. Cosine with `System.Numerics.Vectors` for SIMD. Probe method. Graceful failure on connection refused / timeout / shape mismatch.
7. **Rerank (`Rerank.cs`).** Takes FTS5 results + keyword-extracted query. If semantic enabled, embeds query, loads pool vectors, cosines, re-orders. Returns the final list.
8. **`Program.cs` — orchestrate.** Read stdin, parse JSON, drive the pipeline, format output. All try/catch paths collapse to `{}` + exit 0.
9. **`build-hook.ps1`.** Detects MSVC BuildTools via the workaround I validated today, sets env, runs `dotnet publish`, copies the output to `src/claude_recall/native/`.
10. **`init-hooks` change.** Python side checks for `src/claude_recall/native/claude-recall-hook.exe` existence at init-hooks time; if present, copies it and registers directly in settings.json. Fallback to v0.3 shell-hook path otherwise.
11. **GH Actions wheel build.** `.github/workflows/build-wheels.yml` with two jobs: `build-wheel-win-x64` (with binary) and `build-wheel-pure-python` (without).
12. **Flip `use_in_hook` default** to `true` in `config.py`. Regenerate default-config tests.
13. **Docs + CHANGELOG + release.**

---

## 11. Test plan

**C# side (xUnit):** `src/ClaudeRecall.Hook.Tests/` with one test class per source file.

- `ProjectsTests` — port of the 7 Python `test_projects.py` cases. Identical assertions.
- `KeywordsTests` — port of 13 Python `test_keywords.py` cases.
- `ConfigTests` — 5 cases matching `test_config.py`.
- `StorageTests` — opens the Python-produced fixture DB, runs the same FTS5 queries Python does, asserts byte-identical snippet output.
- `EmbeddingsTests` — mocked `HttpMessageHandler` for Ollama (same role `_FakeOllama` plays on the Python side). Cosine numerical correctness checks.
- `RerankTests` — identical cases to `test_semantic_search.py` with a scripted query vector.
- `ProgramTests` — end-to-end stdin JSON → stdout JSON, including failsafe paths (bad JSON in, empty stdin, missing DB, broken config, Ollama down).

**Python side (subprocess tests):** `tests/test_hook_binary.py`.

- Locate the built `claude-recall-hook.exe` (skip if not present — non-Windows CI).
- Subprocess-invoke with controlled stdin / env / config.
- Assert output JSON is valid and matches the Python CLI's output for the same inputs (regression test: binary ≡ `claude-recall search --project auto --from-config --extract-keywords --semantic-from-config --agent-context`).
- Latency assertion: `claude-recall-hook.exe` < 200ms with Ollama unreachable (FTS5-only path); < 300ms with Ollama warm and semantic on. Measured, not mocked.
- Fault injection: corrupt config, missing DB, Ollama returning 500, all emit `{}` + exit 0.

**CI job:** `windows-latest` runs both the Python suite and the C# suite; cross-validates binary output vs. Python output on fixture data.

**Coverage goal:** ≥ 85% per C# class. Same bar as the Python modules.

---

## 12. Acceptance criteria — v0.4.0 ship readiness

- [ ] `dotnet publish -c Release -r win-x64 --self-contained` produces a working `claude-recall-hook.exe` ≤ 5MB (excluding `e_sqlite3.dll`).
- [ ] Cold-start latency (FTS5-only path, Ollama disabled): < 50ms. Measured on a fresh process on the ANI-scale (25k messages) corpus.
- [ ] Warm-path latency with semantic: < 300ms (Ollama localhost, model pre-warmed). Target is ~80-120ms; 300ms is the hard ceiling.
- [ ] Output JSON is byte-identical to `claude-recall search --agent-context` invoked with the same flags, on a shared fixture corpus.
- [ ] Every failure mode (bad config, missing DB, Ollama down, bad stdin, embedding timeout) produces `{}` and exit 0.
- [ ] `pip install claude-recall` on Windows x64 ships the binary; `init-hooks` drops it into `.claude/hooks/`; `settings.json` references it directly.
- [ ] `pip install claude-recall` on macOS / Linux still works with the pure-Python wheel; hook falls back to the v0.3 shell path.
- [ ] `[embeddings].use_in_hook` default flipped to `true`; semantic is the default hook behavior on platforms that have the binary.
- [ ] Hook-version stamp mechanism still fires stale-hook warnings when upgrading across v0.3 → v0.4.
- [ ] No new runtime deps visible to `pip install claude-recall` (core still zero-dep; binary is self-contained).

---

## 13. Risks and mitigations

| Risk | Mitigation |
|---|---|
| **NativeAOT size drift** — binary grows as features are added | Treat 5MB as a soft ceiling. CI job fails if exceeded. Forces us to keep deps minimal. |
| **MSVC toolchain drift on contributor machines** | `build-hook.ps1` detects the best available BuildTools installation and sets env. `CONTRIBUTING.md` documents the workflow. Verified today against the working VS2022 BuildTools install. |
| **Tomlyn AOT incompatibility** | Verify in step 4 of the plan with a minimal AOT publish test. Fallback is a hand-rolled minimal TOML parser (the config shape is small and flat). |
| **System.Text.Json AOT limitations** | Use source-generated contexts (`JsonSerializerContext`). Standard AOT pattern. No reflection. |
| **Binary and Python drift** — someone fixes a bug on one side, forgets the other | CI cross-validation: a test that invokes both paths on the same fixture and asserts byte-identical output. Runs on every PR. |
| **Schema version drift** | Binary reads `schema_version`; fails gracefully on unexpected values. Hook-version stamp mechanism already flags mismatches. |
| **Users on unsupported platforms think it's broken** | Pure-Python wheel is the fallback; init-hooks behavior mirrors v0.3 on those platforms. `claude-recall status` reports which hook path is active. |
| **Wheel size on PyPI** — 4MB binary + Python = large wheel | Each platform-specific wheel is only downloaded by matching-platform users. Pure-Python wheel stays < 100KB. |
| **GH Actions CI minutes consumption** — per-platform builds add up | Windows-only for v0.4. Add platforms as concrete demand arrives. |

---

## 14. Security considerations

- **Binary reads the same SQLite file as Python** — same privacy surface as v0.3. All data stays local.
- **Ollama calls are localhost loopback by default** — unchanged from v0.3.
- **Binary is read-only against the DB** — `SqliteOpenMode.ReadOnly`. Cannot corrupt the index even with a bug.
- **No new network surfaces.** No telemetry, no update check, no license server.
- **Binary is signed only if it ever ships to PyPI / MSIX / GitHub Releases on a signed workflow.** v0.4 unsigned; v1.0 revisit.
- **Supply-chain surface:** .NET SDK, Microsoft.Data.Sqlite, SQLitePCLRaw, Tomlyn. All Microsoft-maintained or high-trust open source. Versions pinned in csproj.

---

## 15. Design decisions resolved (approved 2026-04-23)

Six questions settled during the kickoff:

1. **Hybrid architecture (C# hook binary + Python CLI)** over full rewrite. Minimum surgery, keeps v0.3 work intact.
2. **SQLite + FTS5 stays** — confirmed by spike. Microsoft.Data.Sqlite bundles FTS5-enabled SQLite; AOT-compatible.
3. **Windows x64 only for v0.4.** Additional platforms post-release, driven by real user demand.
4. **Same-repo layout** with `src/ClaudeRecall.Hook/` alongside `src/claude_recall/`. Separate repo option kept open for post-1.0.
5. **Wheel-per-platform distribution** via GH Actions. Idiomatic Python distribution; one `pip install` command for the user.
6. **Binary registered directly as `command`** in settings.json — no shell wrapper. Simpler registration, faster, no shell-parsing friction.

---

## 16. Open questions for v0.4.1+

1. **macOS + Linux binaries.** Add once someone on those platforms is actively dogfooding. Cross-publish from GH Actions matrix.
2. **SIMD cosine kernel.** `System.Numerics.Vectors` is the floor; if profiling shows rerank as a hot spot at > 1000-candidate pools, look at `Vector256<float>` explicit vectorization.
3. **Signed binary on Windows.** Sigstore or Authenticode. Relevant when we have an audience that warns on unsigned executables.
4. **Warm-daemon mode.** `claude-recall-hook --daemon` running as a long-lived process, with hook scripts as thin IPC clients. Could cut another ~20ms if `~80ms` ever feels slow. Probably not needed.
5. **Fallback to Python CLI when the binary crashes.** Currently the binary's failsafe is "emit `{}` on any error." If a specific failure mode repeatedly bites users, we could have the binary `exec` the Python CLI as a last resort. Deferred.
6. **Port the SessionStart hook too.** Currently the SessionStart hook just runs `claude-recall status --format agent-context` via .sh/.ps1. Not latency-critical. Revisit only if hook-count or hook-contract complexity grows.

---

## 17. Milestones

- **v0.4.0 — this scope.** C# hook binary on Windows x64, wheel distribution, `use_in_hook=true` default. Release as GitHub tag.
- **v0.4.1 — macOS + Linux binaries.** Additional CI matrix jobs. Same architecture, no logic changes.
- **v0.5.0 — PyPI publish.** Gated on v0.4 binaries being stable. First `pip install claude-recall` without `git+https://`.
- **v1.0.0 — API stability.** Lock the CLI surface, binary protocol, hook contracts. Signed binaries. Public announcement.

---

## 18. Starter commands for the implementing agent

```powershell
# 1. Clone the VS2022 BuildTools env setup from build-hook.ps1 (created in step 9)
#    For manual dev:
$msvcDir = "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Tools\MSVC\14.44.35207"
$sdkV    = "10.0.26100.0"
$sdkRoot = "C:\Program Files (x86)\Windows Kits\10"
$env:PATH    = "$msvcDir\bin\Hostx64\x64;" + $env:PATH
$env:INCLUDE = "$msvcDir\include;$sdkRoot\include\$sdkV\ucrt;$sdkRoot\include\$sdkV\um;$sdkRoot\include\$sdkV\shared"
$env:LIB     = "$msvcDir\lib\x64;$sdkRoot\lib\$sdkV\ucrt\x64;$sdkRoot\lib\$sdkV\um\x64"

# 2. Branch
git checkout -b v0.4-hook-binary

# 3. Create the C# project (mirrors the spike)
cd src
dotnet new console -n ClaudeRecall.Hook
# Then edit csproj to match the spike's AOT settings

# 4. Implement in §10 order, running xunit + pytest after each module
dotnet test src/ClaudeRecall.Hook.Tests
pytest

# 5. Local wheel build
./build-hook.ps1                                   # compiles the binary, copies to package data
pip install -e .                                   # installs with binary available
claude-recall init-hooks --force                   # wires the binary into a test project

# 6. Release after §12 green
git tag v0.4.0 && git push --tags
gh release create v0.4.0 --notes-file CHANGELOG.md
```

---

**End of v0.4 hook-binary plan.** The v0.3 `docs/EMBEDDINGS-PLAN.md §16 Open questions (4)` — "Persistent Ollama warming" — is partially superseded by this plan: the speedup comes from eliminating Python cold-import, not from warming Ollama. Ollama stays exactly where it is. Revise that item in EMBEDDINGS-PLAN if/when v0.4 ships.
