# Contributing to claude-recall

Thanks for looking under the hood. This document covers the setup you need to
hack on each part of the repo.

## Repo layout

```
src/
├── claude_recall/               # Python package (CLI, indexer, Python hooks)
│   └── native/                  # Built C# binary lives here (gitignored; CI-built)
├── ClaudeRecall.Hook/           # C# hook binary (v0.4+)
├── ClaudeRecall.Hook.Tests/     # xUnit tests for the C# binary
└── ClaudeRecall.sln             # solution for both C# projects
spike/                           # one-off research projects; safe to delete
tests/                           # Python test suite
docs/                            # design docs (PLAN.md is the source of truth)
```

## Changing Python code only

You need Python 3.11+ and the dev deps. MSVC is **not** required unless you
also touch the C# hook.

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows
# source .venv/bin/activate       # macOS/Linux
pip install -e ".[dev,embeddings]"
pytest
ruff check src/claude_recall tests
```

Submit a PR against `main` with:
- A test covering the change (see `tests/` for patterns).
- Updated `CHANGELOG.md` under "Unreleased" (or the target version's section).
- No new runtime deps on the core install — optional extras are fine.

## Changing the C# hook binary

You need:
- **.NET 9 SDK** — https://dotnet.microsoft.com/
- **MSVC C++ Build Tools** with the `Microsoft.VisualStudio.Component.VC.Tools.x86.x64`
  component. This provides the linker + static CRT (`libcmt.lib`) that
  NativeAOT needs to produce self-contained binaries.

### MSVC install options

- **Visual Studio Community/Professional/Enterprise 2022+**: install the
  "Desktop development with C++" workload.
- **Standalone Build Tools**: https://visualstudio.microsoft.com/downloads/ →
  "Build Tools for Visual Studio 2022" → select the same workload.

Either provides the files at one of:
```
C:\Program Files (x86)\Microsoft Visual Studio\2022\{BuildTools,Community,…}\VC\Tools\MSVC\14.x.x\lib\x64\libcmt.lib
```

### Workflow

```powershell
dotnet test src/ClaudeRecall.Hook.Tests        # xUnit tests (no MSVC needed for this)
.\build-hook.ps1                               # AOT-publish + stage into src/claude_recall/native/
pip install -e .                               # reinstall so package_data picks up the new binary
```

`build-hook.ps1` auto-detects the BuildTools install and sets PATH/INCLUDE/LIB
for the PowerShell session. If detection fails, set them manually per the
top-of-script docstring.

### Known friction

- If `vswhere.exe` doesn't find your BuildTools install (common with
  Chocolatey-style side-loaded installs), `dotnet publish /p:PublishAot=true`
  prints "Platform linker not found". `build-hook.ps1` passes
  `/p:IlcUseEnvironmentalTools=true` which bypasses the auto-detect and
  uses whatever `link.exe` is on PATH.
- If you see `LNK1104: cannot open file 'LIBCMT.lib'`, your MSVC install
  only has the OneCore CRT variant (happens with some VS 2026 Community
  installs). Install the full BuildTools 2022 to get `lib/x64/libcmt.lib`.

## Releasing

Maintainer-only. See `docs/HOOK-BINARY-PLAN.md §10` for the release order.
CI (`.github/workflows/build-wheels.yml`) handles the wheel build — tag a
version, push the tag, GH Actions uploads wheels to the GitHub Release.

## Code conventions

- **Python**: ruff clean, 100-char lines, type-annotated, zero runtime deps
  on core install.
- **C#**: nullable enabled, `TreatWarningsAsErrors=true`, no reflection
  (AOT-safe), System.Text.Json source-generated contexts.
- Cross-language behavior contracts are validated by Python tests that
  subprocess-invoke the C# binary on shared fixture data.

## Questions

Open a discussion on GitHub. This is a small project; there's no cathedral.
