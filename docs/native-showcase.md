# Native showcase follow-up candidate

This is stage 1, layered onto public commit `934fa5301a3e78ca0e41bf3890b0cc991c239e11`
(tree `2ad6955855e990f3d8120fec641ec38954743972`). The original feature source was
prepared from the exact runtime tree of
[6eb8fbd](https://github.com/try-auroraview/auroraview-unreal/commit/6eb8fbd44af951a8812a6df473e83d7a2adc96c5)
(tree `829863926e4a02142058ecb8f2f32b1d21a6bbff`). The baseline native build result
does **not** validate these changes. UHT/UBT/package and graphical Editor gates
must run again for each new source identifier. No screenshot or video is supplied
by the source-only work.

## Updated baseline evidence boundary

The first real graphical `BridgeRoundTrip` at `6eb8fbd` ran: native readiness,
echo and GameThread assertions passed, while the aggregated malformed-type
assertion failed. A later independent Editor exit hit an Array assertion; the
cause remains unresolved and clean shutdown is not claimed.

The strict-type source at
[`934fa530`](https://github.com/try-auroraview/auroraview-unreal/commit/934fa5301a3e78ca0e41bf3890b0cc991c239e11)
passed UE 5.7.4 UAT/UBT/wrapper and complete-package verification. Its corrected
missing/empty/numeric-type CEF checks still await an actual Editor rerun. Every
new feature in this native follow-up stage remains unbuilt and unrun in Unreal;
the baseline results do not validate it.

## Stage 1: real dock ownership and private workspace

`RegisterDocked` registers a stable `AuroraView.View.<Id>` tab type;
`OpenDocked` invokes its real Nomad `SDockTab`. The same browser, reflected
endpoint, reserved ready signal, per-view mailbox and generation from `Open`
are retained. `Open` still creates a floating SWindow. A live ID cannot change
presentation until closed. Close cancels its session synchronously; Remove also
unregisters the tab type. The spawner and host state are removed during shutdown.

`Hide` remains floating-window-only and returns false for a dock. Dock closing,
activation and sidebar operation belong to Slate. This is deliberate API behavior,
not a claim that hiding browser pixels hides an Editor dock.

`AuroraView.Showcase` opens a native `SAuroraViewWorkspace`. Its own `FTabManager`
uses stable Inspector/Outliner/Assets tab types and a versioned layout. It saves
only its own layout in the disposable project's Saved/Config/AuroraViewLayout.ini.
It does not replace LevelEditor's or the global manager's persistence callbacks.
No `FTabId.InstanceId`, SetParentDockTab shim, simulated HTML titlebar or renderer
replacement is involved. All three panels can be closed and reopened by native
buttons. At stage 1 the Outliner/Assets content is explicitly unimplemented.

Register custom dock definitions before attempting restoration. This plugin's
PostEngineInit registration timing versus global startup-layout restoration still
requires installed Editor verification. Automatic global startup reopen is not
claimed; explicitly invoke `AuroraView.Showcase` to restore its private panel
layout. Sidebar relocation is a separate gate and must never be inferred from
successful docking or the existence of an SDockTab.

## Source checks and native test entry points

- `bash scripts/check.sh`: source invariants, pinned unmodified Core hashes,
  standalone mailbox tests and Core transport tests only
- `AuroraView.Editor.BridgeRoundTrip`: original actual floating CEF test
- `AuroraView.Editor.DockedLifecycle`: two independent real docked CEF echo
  replies, close A while B remains ready, same-ID reopen with a fresh generation,
  and removal. Run in a graphical UE 5.7 Win64 Editor, without NullRHI

A native execution owner should save source commit/tree, engine Build.version and
BuildId, compiler, exact Automation log/report, native DLL/package hashes, and
screenshots/video. A source scan or successful standalone test is never a pass
for a native behavior case.

## Stage 1 manual acceptance

For every action, record before/after actual Slate state plus a screenshot/video
timepoint. Mark unavailable implemented tests `blocked`; label missing code
`unimplemented`. Do not label missing features `not_run`.

1. Open showcase. Assert a live registered `AuroraView.View.NativeShowcase`
   SDockTab contains the browser and Core echo returns an actual native reply
2. Dock the whole workspace beside the Level Editor viewport; then undock it and
   redock. Assert the same view generation, working input and working RPC
3. Move the Inspector into a different private stack; resize each split; close
   Assets and reopen it. Assert real Slate panel topology and same CEF generation
4. Save layout, close the workspace, reopen using the console command. Assert
   restored splits/open panels. Restart Editor and explicitly invoke the same
   command; compare private layout. Separately measure global startup auto-reopen
5. Use Toggle eligible tabs in sidebars to move eligible panels
   into temporary native sidebars. Open/close their drawers, then toggle again
   to restore them. Assert actual sidebar membership, same CEF generation and
   working native calls; earlier docking success alone does not pass this case
6. Run DockedLifecycle, original BridgeRoundTrip, then repeat close/reopen under
   garbage collection and exit Editor. Assert no stale callback, orphan tab or
   browser; inspect the complete process exit log

## Reviewed lifecycle repairs included in this stage

Ownership is copied before factory/Slate callbacks and revalidated afterward by
mapped session, browser, native browser, live epoch and tab identity. Removal
retires the map entry before destruction; shutdown snapshots and retires owners
before callbacks. Live presentation epochs are module-unique and zero after
retirement. A newer/replacement presentation cannot satisfy an older open request.

Failed-spawn error tabs receive close callbacks before browser construction.
Retry uses only the still-owned tab; a manually closed error tab is retired even
if a caller retains it strongly. Close/Remove/shutdown also own browserless tabs.

Authored native Automation includes `AuroraView.Editor.DockFactoryReentrancy`
(map growth, self-close, remove/replace, close/reopen) and
`AuroraView.Editor.FailedDockRecovery` (retry and retained manually closed error
tabs). These tests, `DockedLifecycle`, and the strict-type `BridgeRoundTrip` have
not been compiled or executed for this stage. `scripts/check.sh` runs five
static dock/source regression checks in addition to the baseline source-safe
suite. Static markers are not compiler or native behavioral evidence.

Cross-window CEF focus and Chinese IME remain blocked/unverified. Installed
source inspection found stale activation/IME-parent-cache risks; no engine-private
patch or unsupported SetParentWindow-only repair is introduced.

Stage 2 will add typed selection, push events and the real native Assets/Outliner
panels. Stage 3 will add custom Slate drag/drop surfaces and guarded fixtures.
Those later-stage paths are absent from this stage.
