# Experimental UE version selection

**UE 5.7 Win64 Editor is admitted for the first validation build, not declared
supported.** No UHT/UBT or native Editor run has passed for this candidate.

The version gate selects a single intended source-validation target. Exact
source overloads, UHT generation and UBT rules still require a real build of
this candidate against the selected UE 5.7 installation.

## Exact source boundary

The candidate uses `SWebBrowser` with an existing `IWebBrowserWindow`,
`BindUObject`, `UnbindUObject`, `LoadString`, `ExecuteJavascript`, `GetUrl`,
`IsLoaded`, `StopLoad`, `SetParentWindow`, `CreateBrowserWindow`,
`CloseBrowser(true, false)`, browser navigation/load delegates, `FTSTicker`,
`OnEnginePreExit` and `TStrongObjectPtr`.

It does not use `SetParentDockTab`. The engine's supported browser owns rendering
and IPC; no external CEF binary is bundled.

## Target policy

The present patch only changes the source gate and preflight to UE 5.7. Use a
compiler and SDK supported by that exact engine installation, following Epic's
official setup guidance. All other engine minors and non-Win64/non-Editor
targets remain unverified and blocked. A future version requires its own
compile, browser and lifecycle validation; no compatibility is inferred from
shared API names or a shared CEF version.

## Next verification gates

1. Verify the normal repository source commit/tree on the selected executor
2. Read actual `Build.version`, relevant public headers, compiler and SDK again
3. Use normal `RunUAT BuildPlugin` with isolated output; retain UHT/UBT diagnostics
4. Coordinate graphical Editor access and run the full acceptance matrix
5. Keep publication experimental until each claimed platform/version is verified

The existing [official source references](architecture.md#official-unreal-sources-checked-2026-10-05)
describe API design. Some Epic web pages return a different documentation
version or cannot be fetched. The selected installation's headers and actual
build are authoritative; web documentation is not compiler evidence.
