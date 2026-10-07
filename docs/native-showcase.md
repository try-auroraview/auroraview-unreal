# Native showcase follow-up candidate

This is cumulative stage 2, layered onto public commit `934fa5301a3e78ca0e41bf3890b0cc991c239e11`
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
not been compiled or executed for this stage. Stage 1 introduced five
static dock/source regression checks; the cumulative stage 2 suite below
adds typed scope and retained-Outliner guards. Static markers are not compiler or native behavioral evidence.

Cross-window CEF focus and Chinese IME remain blocked/unverified. Installed
source inspection found stale activation/IME-parent-cache risks; no engine-private
patch or unsupported SetParentWindow-only repair is introduced.


## Stage 2: typed native inspector

The showcase replaces the two stage-1 placeholder panels with the engine's
AssetPicker and actor-browser Outliner. Selection from native widgets is sampled
from actual Editor state. Actor selection is observed on GameThread at most five
times per second while the inspector subscribes. AssetPicker selection and the
primary Content Browser's own selection event update the inspected asset data.
The host removes only its own delegate handle at shutdown.

`showcase.subscribe` returns the initial snapshot and enables `showcase.state`
events through the unchanged Core bridge. Every snapshot includes browser
generation, opaque scope, sequence, monotonic host time, actual selected actor IDs
and values. Push refuses unready/closed views. A new browser generation clears
subscription, actor references and restore state; world changes clear opaque IDs
and produce a new scope. The default command owns one showcase view; the fixture CEF test uses its own
independently scoped host/view ID. Each observer does
not intercept or unsubscribe any other view, plugin or global selection observer.

Actor IDs are random, session/world-scoped GUIDs backed by checked weak references.
No browser-provided object path is loaded or resolved. The inspector can select
only actors already observed in that session. Native selection can be rejected
by the Editor, so the response includes observed state and `selectionMatched`.

Only full transforms are editable: finite location ±10,000,000 cm, rotation
±360 degrees (roll/pitch/yaw), and positive scale 0.001–1000. Exactly one selected
current Editor-world actor with a root, unlocked actor transform and unlocked
level is required. PIE,
stale IDs, invalid shapes and stale expected transforms are rejected. Changes use
FScopedTransaction, actor/root Modify, SetActorTransform and PostEditMove, then
return actual host values and a comparison. Restore is a second native transaction
that restores this session's last edit only if no intervening host edit changed it.
It does not issue a generic global Undo command. Native Ctrl+Z must be tested too.
Material, light, arbitrary property editing and remote/eval endpoints are
unimplemented.

Asset reveal calls the actual Content Browser API. Its return status is only
`submitted`: synchronization runs on a later tick and may open a browser window.
A separate actual primary-browser selection observation is required to prove it.
Empty selection is not presented as a working clear-selection operation.

`AuroraView.Editor.TypedInspectorGuards` tests invalid typed requests, opaque-ID
scope and actual current selection snapshots without editing a user's world.
The approved disposable-fixture test adds mutations, Undo/restore and assertions
in the next stage. UI host feedback is not a test pass merely because JSON arrived.

The native actor-browser is explicitly scoped to the current Editor world. Its
container disables itself immediately when that world changes or PIE begins;
the observer retires/rebuilds the widget for the next idle Editor world. It is
released before Editor services teardown. Creating the Outliner does not claim
its later-tick population/selection has already completed.

## Stage 2 boundary and retained-Outliner repair

The reviewed shared host and its native interaction policy are carried intact.
That includes dormant complete-selection drag-admission helpers and feedback;
no custom Slate drag surface calls them in this stage. The native AssetPicker's
own drag capability remains enabled. The actual custom source/drop target,
fixture mutations, acceptance registry and runner arrive in stage 3.

Generation zero or Stop clears every retained Outliner child, including later
refreshes. A new native actor browser is allowed only for a matching live,
nonzero presentation generation and current idle Editor world. Generation/world
and container ownership are rechecked across native construction and installation.
The complete reviewed policy header is tested by 28 executable standalone C++
cases, including closed/mismatched-generation retirement. The actual retained
native-container regression requires the guarded fixture added in stage 3 and
is not present or claimed as run here.

Pristine forms follow actual native single selection and same-actor updates.
Dirty drafts expose keep/discard/reload choices; deselection, multiselection or
truncation disables mutations. All reviewed weak actor/root/world/session
revalidation and nested-mutation guards are retained. Native callbacks may
interrupt a change; an interrupted result never promises an unobserved rollback.

The source-safe suite adds 12 mock-DOM inspector tests, 28 production policy
cases and eight static source guards. None compiles or executes Unreal. All
feature UHT/UBT/package, typed mutation, real selection/push and native Outliner
retirement gates remain `not_run` for this exact stage.
