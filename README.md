# AuroraView Unreal Editor source candidate

**Experimental source candidate: UE 5.7.4 / Win64 native build and package checks passed; actual Editor UI remains `not_run`.**
This is reviewable source, not a supported binary or a completed Unreal release.
No precompiled Unreal binary or stable release is included. The verified build
used [source commit 6eb8fbd](https://github.com/try-auroraview/auroraview-unreal/commit/6eb8fbd44af951a8812a6df473e83d7a2adc96c5).
UHT, C++ compilation, DLL linking and UAT packaging completed successfully.
Required bridge assets and license notices were hash-checked in the final package.
Real CEF rendering/RPC, lifecycle, GC and Editor shutdown acceptance are still pending.
See the [verification matrix](docs/acceptance.md).

## What is here

- An Editor-only `.uplugin` and UnrealBuildTool module
- Native `SWindow` / `SWebBrowser` integration using the engine's own CEF backend
- Unmodified upstream AuroraView bridge assets pinned to
  `11b3a29ad95a46cb22aaa604614de16da16bfc22`, with MIT notice and SHA-256 manifest
- A small `window.ipc.postMessage` transport to `window.ue.auroraview.postmessage`
- A separately acknowledged, reserved native ready signal immune to RPC queue saturation
- One independently owned browser, reflected endpoint, queue and generation per view
- GameThread-only host dispatch; show, hide, close, same-ID reopen and removal
- Bounded queues, stale-generation rejection, duplicate-delivery suppression,
  replaceable host handlers, nonblocking browser close and shutdown cleanup
- A working cloud test entry point and an Unreal-only real browser smoke test

Independent source review found and corrected startup backpressure and two
upstream-stub compatibility defects. A small `ue_bootstrap.js` adapter rejects
unsupported early `invoke` and resolves early `whenReady` using Core's existing
event; the two pinned Core assets remain byte-for-byte unchanged. See the
[source review record](docs/source-review.md).

The renderer is UE WebBrowser/CEF. The Rust WebView backend, PyO3, Python, Qt,
and arbitrary editor-command evaluation are not involved. The existing Core
bridge remains the owner of JavaScript promises, IDs, timeouts, API binding and
event delivery. The Unreal adapter decodes its existing wire envelope into
explicitly registered C++ host handlers; it is not a second Core implementation.

## Source target and ABI boundary

The current **experimental source validation target is UE 5.7 / Win64 Editor**.
`Build.cs` deliberately rejects other engine minors and platforms. The original
reviewed source snapshot provisionally targeted 5.6; the current gate and
preflight select 5.7 for experimental validation. The first native build exposed
two C++ issues and a packaging omission; each was corrected in a separate commit.
Pinned Core bridge JavaScript remains unchanged.
This is not a source-compatibility or binary-support claim. See the
[version validation plan](docs/version-validation.md) and
[verified source provenance](docs/provenance.md).

The verified native build used the actual UE 5.7.4 Win64 installation. Build each
binary with that engine's UBT/UHT, its compiler/toolchain, and its CEF binaries.
Do not reuse DLLs across UE minors, BuildIds, architectures or custom engine builds.

There is no packaged-game, UE4, ARM64, Linux, macOS, Python ABI, live reload or
arbitrary remote URL compatibility claim. Dynamic DLL reload is explicitly
disabled; enabling/disabling the plugin requires an Editor restart. The module
still removes its ticker, native bindings, browser ownership and pre-exit delegate
on normal shutdown.

## Run the checks available without Unreal

```sh
bash scripts/check.sh
```

Requires C++17 compiler, Node.js and Python 3; no third-party package installation.
The C++ tests compile the exact transport mailbox used by the plugin. Node tests
exercise the vendored **unmodified Core bridge** plus the actual UE transport JS
in isolated VM contexts. These are contract tests, not a fake Unreal build.

## Prepare a real engine build

1. Use the existing, authorized UE 5.7 Win64 installation selected for validation. Do not infer a path
   from a stale engine registration. Read `Engine/Build/Build.version` first.
2. Run `python scripts/preflight_engine.py --engine-root "<UE root>"` and save
   its version/header inventory. This command does not install or compile anything.
3. Compare the public API signatures with the exact installed headers. In
   particular verify the `SWebBrowser` constructor, browser delegates,
   `CloseBrowser`, UHT endpoint and `ReadOnlyTargetRules.Version` access.
4. Build using that engine's `Engine/Build/BatchFiles/RunUAT.bat BuildPlugin
   -Plugin="<source>/AuroraView.uplugin" -Package="<separate output>"
   -TargetPlatforms=Win64`. A clean UHT/UBT pass is a required next gate.
5. In a disposable Editor project, place this source under `Plugins/AuroraView`,
   enable it and restart. Run `AuroraView.Demo` in the Editor console.
6. Run the `AuroraView.Editor.BridgeRoundTrip` Automation test in a real
   graphical Editor, without `-NullRHI`. Then complete [the acceptance matrix](docs/acceptance.md).

This document does not authorize installing Unreal, accepting Epic terms,
changing a user's project, using the user's computer, or publishing anything.

## Native API

Include `AuroraViewEditorModule.h` from another Editor module, depend on
`AuroraViewEditor`, and obtain the module through `FModuleManager`.
Call all host-control methods on GameThread:

```cpp
const FName Id(TEXT("MyTool"));
Module.BindCall(Id, TEXT("api.echo"), [](const TSharedPtr<FJsonValue>& Params) {
    return FAuroraViewReply::Success(Params);
});
FString Error;
Module.Open(Id, TrustedHtmlFragment, FText::FromString(TEXT("My tool")), Error);
Module.Hide(Id);   // Browser/session remains alive; RPC remains available.
Module.Show(Id);
Module.Close(Id);  // Cancels queued native work and discards this browser.
Module.Open(Id, TrustedHtmlFragment, FText::FromString(TEXT("My tool")), Error);
Module.Remove(Id); // Also forgets registrations and permanently stops this session.
```

The trusted HTML fragment may use inline CSS/JS and data images; network requests,
frames, popups and subsequent navigation are blocked. Wait for `auroraviewready`
or queue calls with the upstream stub. Use `auroraview.call('api.echo', params)`;
register an `api` shorthand explicitly with Core's `_registerApiMethods` if needed.
The minimum host contract accepts `call`; unsupported `invoke` receives a
structured error. General frontend event callbacks are not implemented.

Handlers must be short, synchronous and nonblocking. Return a JSON value or a
`FAuroraViewReply::Failure`; UE generally disables C++ exceptions. Capture weak
UObjects and validate them before use. No Rust or Python async callback ABI is
introduced. A JavaScript timeout does not retract an already accepted native
call; close/removal/shutdown cancel queued calls. Long-running cancellable jobs
need a future shared Core cancellation/completion contract.

## Evidence and remaining work

- [Architecture and source references](docs/architecture.md)
- [Acceptance matrix and verification limits](docs/acceptance.md)
- [Cloud check output](evidence/cloud-checks.log)
- [Pinned upstream asset manifest](ThirdParty/AuroraViewCore/manifest.json)

No UE headers, engine binaries, CEF redistributables, generated UHT files or
third-party plugin code are included. MIT Core notices are retained. Other
software remains under its respective license.
