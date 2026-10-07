# Build and validate locally

The explicit Win64 source/build candidates are UE **4.18, 4.26, 5.5, 5.7 and 5.8**, in both Editor and Game targets. Use an existing authorized installed engine and its supported Visual Studio/Windows SDK. No script installs Unreal or changes shared engine/user configuration.

## Source contracts

```powershell
pwsh -File scripts/check.ps1 -Python python -Compiler clang++ -Node node
```

On Linux use `bash scripts/check.sh`. These checks exercise portable native guards, both pinned and Chrome 59 bridge variants, Python TCP behavior and build/package failure cases. They do not compile Unreal. Vendored and generated bridge bytes bypass Git text conversion. `python scripts/build_legacy_bridge.py --check` verifies provenance offline; regeneration requires the manifest's exact esbuild version.

## Native compilation and package verification

```powershell
vx python scripts/build_plugin.py --engine-root "$env:UE_ROOT" --engine-version 5.7 `
  --require-clean --output "$env:AV_BUILD_OUTPUT"
```

Use a new output directory outside the checkout and engine. Plain Python works too. The wrapper reads actual engine headers/version, runs UAT BuildPlugin, and verifies both Editor DLLs, their module BuildId, descriptor, resources and licenses. It also checks the actual Development and Shipping Game manifests and native Runtime static/precompiled products. Game compilation alone is not cooked-game acceptance.

The output contains `Package/`, `HostProject/`, `uat.log` and `build-receipt.json`. Source commit/tree/file hashes, actual compiler/SDK log evidence, engine BuildId and package hashes bind each run. Source or engine changes, UAT failures, missing products and altered input configuration fail the run. Legacy UBT's missing compiler log is explicitly recorded. Retained HostProject evidence sits outside the distributable package.

Engine-specific flags handle legacy UAT/VS selection and modern compiler/executor selection. Settings affect only the child process; existing shared UBT XML files are hashed before and after, never edited.

## Editor automation

```powershell
pwsh -File scripts/validate_editor.ps1 -EngineRoot "$env:UE_ROOT" `
  -Package "$env:AV_BUILD_OUTPUT/Package" -Output "$env:AV_EDITOR_OUTPUT"
```

Use a fresh output directory. The validator verifies the exact package/source/engine, creates its own disposable project, prepares and saves a real map with the native AuroraViewPrepare commandlet, and runs nine expected Automation tests. Installed Editor Python is enabled where available; UE4.18 needs none. The suite exercises real rendering, CEF, Slate and native reflection. It requires a working interactive Windows desktop. Zero errors, all expected results and normal process exit are mandatory.

## Packaged Game

```powershell
pwsh -File scripts/validate_game.ps1 -EngineRoot "$env:UE_ROOT" `
  -Package "$env:AV_BUILD_OUTPUT/Package" -Output "$env:AV_GAME_OUTPUT" `
  -Python python -TimeoutSeconds 2400
```

The validator creates an isolated native Game project, explicitly builds its Editor target for cooking, then runs actual BuildCookRun to compile, cook, stage, archive and package the Game. The Editor build writes only this disposable project's outputs and disables IDE hot reload, preserving other open Editors. Compiler logs and process receipts are retained for both stages. It verifies the real Win64 executable/target receipt, matching Runtime resources and installed CEF files, and absence of Editor binaries. It starts that executable with NullRHI and binds the SDK to its PID, engine and Game context. Native world discovery/UFunction invocation, a reverse Python tool call, events and native graceful shutdown must all succeed with process exit zero. Tokens are redacted from retained command lines/logs. Only owned processes may be cleaned up after failure.

`evidence/game-validation.json` is independent of the plugin build receipt. Add `-RenderedBrowser` (or `--rendered-browser` for Python) on an interactive desktop to validate real CEF Core call/invoke to Python, bidirectional events, readiness and close/remove. The default NullRHI run does not establish rendered Game browser acceptance. Rendering, manual docking, native mouse drag destinations and timestamped media cases remain separate.

## GitHub Actions and runners

Hosted source checks run for all PRs. Native same-repository PR/main jobs use the dedicated `auroraview-unreal` label on a Windows self-hosted runner. The five-version matrix serializes jobs and uses the exact local build and Game validation scripts, retaining package, logs and acceptance evidence. Fork code stays on hosted source checks until a maintainer imports the reviewed branch.

The dedicated runner can share the machine, installed Unreal engines, compiler and SDK with dcc-mcp-unreal. Repository-scoped runner registrations remain separate across the two owners. Keep independent service/working directories; do not repurpose the existing runner registration. Native compilation and NullRHI Game acceptance in a service do not certify interactive Editor rendering.
