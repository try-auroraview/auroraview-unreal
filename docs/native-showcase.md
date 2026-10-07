# Native showcase follow-up candidate

This is cumulative stage 3 of the native follow-up series, layered onto public
commit `934fa5301a3e78ca0e41bf3890b0cc991c239e11` (tree
`2ad6955855e990f3d8120fec641ec38954743972`). The reviewed feature source was
originally prepared from the exact runtime tree of
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

## Stage 3: genuine native drag surfaces and fixture

The inspector's surrounding Slate strip has real drag sources. Pointer movement
enters `OnDragDetected`, then returns a native `FReply::BeginDragDrop` with actual
weak selected actors or actual inspected asset data. An HTML button or delayed
RPC never pretends to produce that FReply. Native AssetPicker drag is also left
enabled. The native inbound drop target accepts supported direct actor/asset
operations and inspects their real payloads. A `started` event proves a native
operation began, **not** that the destination accepted it. A target-accepted event
proves only this inspector consumed that payload. Viewport placement or Outliner
reparenting still needs actual world/attachment observations and Undo assertions.
Composite/folder payloads are explicitly gated until their native child operation
contract is implemented and verified; they must not be described as passed.

The project scaffold under `examples/AuroraViewNativeFixture` is content-free.
It references the installed engine's built-in Cube by path; it redistributes no
engine asset, header, binary or generated file. Prepare a new disposable copy,
copy the newly built plugin package into Plugins/AuroraView, create a new empty
map, and save it as `/Game/AuroraViewAcceptance/<SavedMap>` inside that disposable project. Never point this at a user map. Supply `-FixtureMap /Game/AuroraViewAcceptance/<SavedMap>` to the runner; it passes both the explicit startup map and `-AuroraViewFixtureMap=<package>`. The native fixture guard checks that exact package is loaded before any mutation. Installed Editor startup behavior still requires a native check.

Fixture creation requires BOTH exact project name `AuroraViewNativeFixture` and
explicit Editor flags `-AuroraViewAllowFixtureMutations` and `-AuroraViewFixtureMap=<package>`. It refuses a different loaded map, PIE and
engine-owned maps. Three tagged real static-mesh actors are created transactionally.
An existing fixture is reused only after unique A/B/ground role tags, native static-mesh actor type and the installed Cube mesh match. Results use deterministic role order. Duplicates, substitutions and partial fixtures fail closed; existing transforms are preserved.
Nothing is saved automatically, and the fixture is not silently recreated over
modified content. Start again with a new empty map for a clean acceptance run.

Run `AuroraView.Showcase.CreateFixture`, frame the three real actors in the
viewport, switch the viewport to Unlit for consistent geometry visibility, and
open `AuroraView.Showcase`. The initial scene is intentionally simple
and reproducible, with visible cube A, cube B and a floor. For a native outbound
asset case enable Show Engine Content in the native picker's options and select
the installed Cube in /Engine/BasicShapes; for actor
reparenting use A and B. Undo each placement/reparent before the next case.

`AuroraView.Showcase.FixtureTransform` validates actual native object state before
and after a typed edit, stale conflict, own restore, native Undo and native Redo.
It requires the same explicit fixture guard.
`AuroraView.Showcase.FixtureBridgeRoundTrip` uses an independent actual docked CEF
session to call typed snapshot/edit/restore handlers, compare the native UObject,
and verify real Core host-push events. These tests are not a browser simulation.
`scripts/run_native_acceptance.ps1`
provides a graphical Automation entry point for the prepared isolated project.
It does not build/install the plugin or replace manual native drag media proof.

## Evidence contract

`acceptance/cases.json` supplies repeatable steps and expected native states for
every case. It starts with no measured actuals and no media. Use
`python scripts/native_evidence.py --seed <new evidence.json>` after freezing the
source. Record actual source commit/tree, engine version/BuildId, build/DLL hash,
Automation report, per-case real observations, assertions, and exact UTC/host/video
times in the run copy. Keep file references relative to the evidence directory.

Each screenshot/video entry contains kind, file, SHA-256, host_time_s and, for a
video, video_time_s. The recording header carries started_utc and
host_seconds_at_video_zero. Show actual Editor chrome, the native cursor/payload,
the destination, and the resulting host state; keep failures and retries visible.
Do not manufacture frames, animate a stand-in, infer a drop from a browser event,
or label unimplemented features not_run. Source candidates awaiting native
execution are pending or blocked, not native passes.

`python scripts/native_evidence.py --validate <evidence.json>` rejects a claimed
pass without source/engine/build identity, actual native assertions, timestamps
and hashed existing media. `--require-complete` additionally requires every
required case to pass. Deliberately unimplemented optional extensions remain
visible. This validator checks an evidence record; it cannot prove that an honest
native observation was made. Independent review of actual logs/media is required.

The verified private sidebar toggle is now implemented in source. It affects all
eligible tabs or restores remembered tabs according to native manager state.
At least one tab stays; a previously sidebared tab moved to a different manager
may be restored in its new stack. Native
sidebar and layout behavior remain pending actual engine validation. See the
[public API audit](native-api-contracts.md) for exact-version caveats.

## Failed-open recovery and remaining native parent gate

Failed spawns keep a managed native error tab. OpenDocked retries browser
construction in that same still-owned open tab; a manually closed error tab is retired even while another caller holds a strong reference. Reopening then creates a new live tab; Close/Remove/shutdown retire it even if no browser
was ever created. `AuroraView.Editor.FailedDockRecovery` uses an explicit
test-only pre-browser failure seam, then verifies an actual CEF reply after retry
and removal of another failed tab. Rebinding a closed registration recreates the
spawner display name, and every successful/error tab receives its current title.

Actual Slate ownership does not by itself prove CEF native dialog/focus/IME
parenting after docking into a different native window. That source/runtime gate
is explicitly pending; no unsupported native-parenting behavior is claimed.

Initial subscription reads the current primary Content Browser selection. Later
snapshots name the actual asset observation source (picker, primary browser event,
initial primary snapshot, or native drop) and its host timestamp. A submitted
reveal is not promoted to success by reusing an earlier picker observation.

An extracted source archive has no local git identity. The evidence seed leaves
commit/tree/dirty unrecorded in that case rather than inheriting an unrelated
parent project's git HEAD. Supply them only from independently verified delivery/run inputs with an exact source-file manifest, hashed archive and hashed receipt. The validator verifies the archive contents and unpacked files against that manifest. A parent project's commit is not this plugin source identity.

## Cross-window input is a blocked acceptance gate

An installed UE 5.7 source audit found a concrete risk, not a reproduced runtime
failure: SWebBrowserView can update its cached native parent on paint without
rebinding old/new window activation delegates, and the IME context can continue
using an old still-live cached Slate/native window. Public SetParentWindow does
not demonstrate a complete repair. This candidate does not patch engine-private
CEF/Slate state or claim that a relocation callback fixes it.

Same-window native docking remains the implemented path. Cross-window focus,
Chinese IME and lifecycle are visibly marked unverified in the inspector and
blocked in the acceptance seed. The dedicated case keeps two windows alive,
tests composition/candidates/commit/cancel and activation, then closes the old
window and repeats the move. Use an already configured input method; unavailable
IME is a blocker, not a passing test. Retain all native behavior and media.


## Independent-review repairs (source v2)

The original source candidate and delivery remain immutable. These repairs are
additional source changes, not a native rerun. Public PR1 at `934fa530` retains
its separate corrected-source UAT/UBT/wrapper and complete-package pass, the
first graphical baseline smoke’s mixed results, and pending corrected strict-type
CEF rerun; none is evidence that this showcase tree was built or run. Publication
must three-way layer the feature series onto that current public base and preserve
its README/acceptance build evidence. Do not replace the public PR1 tree wholesale.

- Dock ownership is copied before public factory/Slate callback boundaries and
  revalidated afterward by mapped session, browser, native browser, live epoch and
  tab identity. Removal detaches the map entry before destruction. Stop snapshots
  the owners before callbacks. The public live presentation epoch is module-unique
  across removed/recreated IDs and zero after retirement
- Error-tab close callbacks are installed before any failing browser path. Native
  Automation now includes retained manually closed error tabs and content factories
  that grow the session map, close themselves, or remove/reopen their own ID
- Pristine transform forms follow actual single native selection and same-actor
  host updates. Dirty drafts keep their stale expected-transform guard and expose
  keep/discard/reload choices. Deselect/multiselect disables mutation; changed
  world or browser scope invalidates the old draft
- Native transform/selection callbacks revalidate weak actor/root/world/session
  state before subsequent dereferences. Interrupted mutation reports actual-state
  uncertainty rather than promising rollback; native Undo remains available where
  the transaction survives. Nested mutations are rejected. A destruction/reentrancy
  reproduction against installed headers/runtime is still required
- Retained Outliners are cleared and generation/stop gated. Actor history evicts
  unselected entries when full; snapshots report actual selection count and
  truncation. A truncated selection cannot enable single-actor form edits
- Evidence validation v2 is registry-pinned and fail-closed. See
  [the evidence v2 contract](native-evidence-v2.md). No synthetic validator fixture
  is native evidence, and no screenshot/video is supplied by this repair

Native gates remain pending for this exact candidate: UHT/UBT clean packaging,
installed public API compatibility, CEF bridge and strict-type smoke, all native
Automation including new regressions, sidebar/private panel transfer semantics,
fixture map startup/roles and actor callbacks, docking/drag/Undo, normal shutdown,
resource cleanup, and cross-window focus/Chinese IME. No engine-private API or
unverified cross-window fix was invented.


## v3 delta: complete actor-drag admission and retired Outliners

Native outbound actor dragging no longer uses the inspector's 128-entry sample.
It reads every native selection slot, including null entries, and admits only an
entire valid, non-destroying, same-idle-world actor set within the explicit 128-actor
capacity. Any invalid/non-actor/foreign-world entry or overflow rejects the whole
operation. Feedback names the rejection, full selected/eligible counts and limit;
no accepted prefix is passed to native reparenting.

Preparation snapshots the owned live presentation epoch, world, every selected
identity and weak actor reference. It revalidates after RecordNativeDrag's scope
refresh and again after native drag-operation construction, immediately before
BeginDragDrop. The final feedback write has no scope refresh or widget teardown.
Changing selection, world, readiness or epoch, or invalidating an original weak
actor, rejects the whole operation. Destination acceptance and native Undo still
need their own observed native gates.

Generation zero clears every retained native Outliner child. Neither later ticks
nor a different idle world can create an actor browser until the host owns a live
nonzero generation. Rebuild also rechecks generation/world across native child
creation and content installation.

`NativeInteractionGuards.h` is the production, engine-independent admission policy;
28 standalone C++ regression scenarios execute under `scripts/check.sh`. These are
policy tests, not native execution. New authored Automation
`AuroraView.Showcase.NativeInteractionGuards` creates 129 temporary native actors
only inside the guarded isolated fixture, tests the 128/129 boundary, weak-payload
invalidation and callback-boundary changes, then destroys the temporary actors.
It holds actual SBox containers strongly across close/remove and checks their null
children plus an unchanged native actor-browser construction count. No native
Automation or new feature build has been run for this tree.

Public API references: [USelection::Num](https://dev.epicgames.com/documentation/unreal-engine/API/Editor/UnrealEd/USelection/Num)
and [USelection](https://dev.epicgames.com/documentation/unreal-engine/API/Editor/UnrealEd/USelection)
document indexed selection access and possible null slots. The available public
pages resolved to 5.8; the installed UE 5.7.4 `Num() const` and `GetSelectedObject(const int32) const`
signatures were subsequently checked during publication preparation. Other
installed API compatibility and all native behavior remain verification gates. No engine-private API is used.

## Publication-stage verification scope

The cumulative product code is byte-identical to reviewed source-v3 commit
`897823ecd3bdc87b7867274f5c1643bd3d2065e7`; README and baseline acceptance evidence
are reconciled onto public `934fa530`. The earlier in-repository
`evidence/native-showcase-source-checks.log` is historical source-only evidence,
not a claim that this cumulative tree or any native feature was built. Run
`scripts/check.sh` for the current suite: 9 source invariants, 4 preflight cases,
25 evidence cases, 11 static source guards, 11 mailbox cases, 28 production
interaction-policy cases, 25 Core Node tests and 12 mock-DOM inspector tests.

Every new feature UHT/UBT/package and native Automation/graphical gate remains
`not_run`. The historical baseline build/package passes, mixed first graphical smoke
results and pending corrected strict-type CEF rerun are recorded separately in README and the baseline verification matrix.
