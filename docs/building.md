# Build and validate locally

The admitted target is **UE 5.7 Win64 Editor**. Use an existing authorized
installed engine, Visual Studio C++ toolchain and Windows SDK. These commands
do not install Unreal or change its configuration. DLLs must match the exact
engine BuildId; no other Unreal version is admitted by this plugin.

## Source contracts

On Linux, run `bash scripts/check.sh`. On Windows, install Python 3, Node.js
and LLVM, then run:

```powershell
pwsh -File scripts/check.ps1 -Python python -Compiler clang++ -Node node
```

Each executable parameter also accepts an absolute path. These checks exercise
the real portable C++ mailbox/interaction guards, bridge JavaScript, source
contracts and build-wrapper failure cases. They do not compile Unreal.
Vendored Core files bypass Git text conversion so their pinned hashes survive
Windows checkouts.

## Native compilation and complete package verification

Use a new output directory outside both the checkout and engine directories:

```powershell
vx python scripts/build_plugin.py --engine-root "$env:UE_ROOT" --output "$env:AV_BUILD_OUTPUT"
```

Plain `python` works too. The command checks the actual engine version and
headers, runs UAT `BuildPlugin` with strict includes, and verifies the packaged
Win64 Editor DLL, module BuildId, descriptor, resources, pinned bridge assets
and licenses. It records the actual source commit/tree and working-file hashes,
compiler, SDK, engine identity and package hashes. It fails if those inputs
change during the build, if UAT fails, or if any required artifact is missing
or changed. Existing nonempty output directories are never overwritten.

The output contains `Package/`, `uat.log` and `build-receipt.json`. A successful
receipt proves native compilation and package integrity. Editor acceptance is
recorded separately. UBT compiler selection and executor settings are passed
only to the child process; no shared engine or user configuration is edited.

## Editor automation

After a successful build, run:

```powershell
pwsh -File scripts/validate_editor.ps1 -EngineRoot "$env:UE_ROOT" `
  -Package "$env:AV_BUILD_OUTPUT/Package" -Output "$env:AV_EDITOR_OUTPUT"
```

Use another new directory for `AV_EDITOR_OUTPUT`. The command creates its own
disposable `AuroraViewNativeFixture` project, copies the verified package,
creates and saves a dedicated acceptance map, and runs all eight `AuroraView.`
Automation tests. Map preparation uses the engine's Python plugin; the plugin
itself has no Python dependency. The test run uses normal rendering because
the suite includes actual CEF and Slate. An interactive Windows desktop and
working graphics/CEF runtime are required.

The process must exit successfully and the exported report must contain every
expected test with zero failures or unrun tests. Missing reports, timeouts and
incomplete suites fail. Preserve the logs and reports when diagnosing a failure.
Manual docking, native mouse drag destinations and timestamped media cases in
[the acceptance matrix](acceptance.md) remain separate from automated checks.

## GitHub Actions

`source-checks.yml` runs the portable contracts on a hosted Linux runner.
`build-uplugin.yml` follows the `dcc-mcp-unreal` native-build pattern: an
installed Windows self-hosted runner, UAT compilation, complete package checks
and retained artifacts. The runner needs labels `self-hosted`, `Windows`, `X64`
and `auroraview-unreal`, access to the workflow's UE root and Visual Studio/SDK.
The dedicated label keeps this repository's jobs on its own runner.

Same-repository PRs and main pushes run the native build. Fork PRs run hosted
source contracts until a maintainer reviews and imports the branch. CI retains
the package, complete build log and receipt for 14 days. Graphical Editor
automation runs locally; a service runner's native compilation result does not
certify an interactive desktop or manual acceptance.
