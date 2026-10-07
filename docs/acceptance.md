# Verification matrix

Source/transport checks were rerun on 2026-10-07. Actual UE 5.7.4 Win64 build
and complete-package verification passed at
[6eb8fbd](https://github.com/try-auroraview/auroraview-unreal/commit/6eb8fbd44af951a8812a6df473e83d7a2adc96c5).
Editor/CEF execution remains untested. Earlier stress/sanitizer evidence is
retained with its original scope. Never substitute source or build checks for
native Editor execution.

| Gate | Status | Evidence / required result |
|---|---|---|
| Unmodified Core asset hashes / retained MIT notice | pass | `verify_source.py`, pinned manifest |
| Descriptor, source target, dependency and source guard checks | pass | 9 source checks total |
| Read-only engine preflight regression | pass | 4 fixture-based cases; no real-engine claim |
| Native mailbox C++17 compile with warnings as errors | pass | GCC 14.2.0 on Linux; mailbox only |
| Mailbox lifecycle/concurrency | pass | 11 cases in `mailbox_test.cpp`, including saturated control delivery |
| Mailbox repeated concurrency regression | pass | All 11 cases repeated 100 times |
| Mailbox ASan + UBSan | pass | `detect_leaks=0`; no sanitizer finding in these tests |
| LeakSanitizer | blocked | Executor ptrace prevents LSAN; no leak-clean claim |
| Actual upstream bridge + UE transport contracts | pass | 25 Node 24.19.0 VM cases, including actual startup order and ready failures |
| Exact engine/platform and build identity | pass | UE 5.7.4 Win64; engine BuildId and produced DLL hash verified |
| UHT reflected endpoint generation | pass | Four generated files in the actual UE 5.7.4 run |
| UBT Win64 Editor plugin compile/link/package | pass | Nine compile/link/metadata actions; UBT, UAT and wrapper exited 0 |
| Packaged runtime assets and license notices | pass | Nine final resource hashes matched; all five runtime file-read paths covered |
| Real Editor visible native view | not_run | Open demo; screenshot with Editor chrome and bridge ready |
| Actual CEF → UObject → GameThread → CEF round trip | not_run | `AuroraView.Editor.BridgeRoundTrip` must pass |
| Native invalid-type structured error path | not_run | Added to real Editor smoke; cloud only checked source guard |
| Host result, structured error, Unicode/escaped JSON | not_run | Demo + additional typed host calls |
| Hide/show preserve one browser and correct visibility/focus | not_run | Repeat 20 times; no extra process/window growth |
| Titlebar close / programmatic close / close before ready | not_run | No crash, stale callbacks or queued host mutation |
| Same-ID reopen | not_run | Repeat 20 times; new generation/token; handlers retained once |
| Two concurrent instances | not_run | Independent content, results, visibility and close |
| Rebind/unbind while open | not_run | New handler replaces old; unbound method returns error |
| Pending call on close and delayed old response | not_run | Promise cancelled where page still exists; no new-page delivery |
| Queue overload, duplicate request, stale endpoint | not_run | Reject overload; never invoke mutation twice |
| Startup failure / missing CEF / missing assets | not_run | Explicit error; no misleading supported/ready state |
| Malformed message / unsupported invoke | not_run | No crash; structured rejection when a usable ID exists |
| Navigation, iframe, popup and external resource controls | not_run | No host bridge leakage; initial LoadString still works |
| Direct native endpoint from blank/srcdoc/data frames | not_run | Test permanent binding boundary, not just JS helper refusal |
| Per-document origin / CEF browser context and storage | not_run | Unique origins implemented; storage isolation not claimed |
| Bound bool future shape and callback ordering | not_run | Real native true/false/rejection before and after RPC result |
| Browser self-close / renderer termination | not_run | Detect and recover stale ready state; no health guarantee yet |
| Disable/re-enable plugin with pending calls | not_run | Restart boundary; no claim of hot DLL unload support |
| Module shutdown cleanup | not_run | Ticker/delegates/bindings/CEF references released |
| Host exit while calls are queued | not_run | No hang, UAF, host API after exit or leftover owned window |
| Actual UE memory/CEF leak check | not_run | Track native resources across open/close cycles |
| GC pressure during pending UObject calls / nonblocking close | not_run | Requires actual engine binding ownership and teardown evidence |
| Mac/Linux/runtime-game/other engine minor | unsupported | Not in source gate or this acceptance scope |

## Native build progression (2026-10-07)

- Initial native build: UHT passed; UBT rejected the nonexistent FJsonValueBool
- First correction: FJsonValueBoolean and its explicit header, confirmed against the installed engine
- Next native build: the boolean error cleared; UBT found incomplete-FImpl cleanup from the inline implicit constructor
- Second correction: declare the constructor publicly and default it after FImpl is complete; retain the out-of-line destructor
- The following native compile/link/UAT run exited 0, but its package omitted required Core bridge assets and notices
- Third correction: Config/FilterPlugin.ini explicitly includes the Core assets, root license and the filter itself
- A fresh complete run at 6eb8fbd passed UHT/build/link/package and independent source/output/resource hash checks; its process exited normally

The known WriteMetadata `Invalid args` LogWarning is retained in the evidence;
this is not a warning-free-build claim. No existing output package was manually
patched. No graphical Editor, actual CEF RPC, GC or native lifecycle test has
passed yet. The source checks and Node/C++ transport fixtures do not replace
those remaining gates.

## Execution order for the future real-engine owner

1. Inventory exact engine + public headers, architecture, toolchain and CEF;
   do not bypass the experimental 5.7 source gate to force another version through
2. Compile with clean intermediate files; resolve UHT/UBT issues before opening
   a project; keep builds matched to that exact engine
3. Launch a disposable project in a graphical Editor, run the actual smoke
   test and retain the automation JSON/log plus visible evidence
4. Validate repeated and interrupted lifecycle paths and two simultaneous views
5. Validate security boundaries, overload/replay, exit and resource cleanup
6. Only after all relevant gates pass, describe the verified engine/platform
   combination as supported; broaden to another version/platform separately

## Important interpretation limits

- Mailbox thread tests prove queue/generation behavior of production mailbox
  code, not Unreal scheduling or the CEF callback thread behavior
- Node uses a controlled `window.ue` endpoint; it proves compatibility with
  the actual Core JS code but does not prove Epic's native binding return shape
- Static checks establish presence and pinning, not compiler correctness
- Close cancels native queued work immediately; JavaScript cancellation delivery
  during native destruction is best effort and requires the real teardown tests
- Core's request timeout rejects its Promise but does not retract host mutations
  already admitted to the queue; do not promise timeout-based side-effect rollback
- The default sample only exposes read-only/echo diagnostic methods. Registering
  destructive editor methods requires its own explicit application design
