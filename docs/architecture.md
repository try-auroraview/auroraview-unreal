# Native architecture and ownership

## Minimal path

`upstream bridge stub → UE startup compatibility shim → trusted fragment → UE transport → Core event_bridge.js`

`window.auroraview.call → window.ipc.postMessage → bound UObject PostMessage`

`bounded per-view mailbox → GameThread ticker → explicit C++ handler`

`JSON reply → ExecuteJavascript → auroraview.trigger('__auroraview_call_result', reply)`

The last event and envelope are unchanged from Core. Requests are `{type:'call',
id,method,params}`. Results are `{id,ok:true,result}` or
`{id,ok:false,error:{name,message,code}}`. No JSON-RPC 2.0 claim is made: this
is AuroraView's existing wire protocol. The endpoint's asynchronous CEF return
only acknowledges queue admission; it is not the RPC result.

Core's `__auroraview_ready` event is translated by the UE transport to a
token-checked `MarkReady` endpoint with a separate reserved lifecycle slot.
Loaded, load-error and ready controls are coalesced independently from the
256-entry RPC queue. A rejected ready acknowledgement reports a fatal backend
error through Core instead of silently losing the signal.

## Ownership

- Module owns the view registry, console command and removable GameThread ticker
- View owns SWindow, SWebBrowser, IWebBrowserWindow, strong UObject reference and
  independent mailbox; browser delegates capture only mailbox/generation
- Endpoint has an immutable mailbox reference, generation and per-document token
- Close invalidates queue admission, clears queued work, unbinds the UObject,
  removes Slate content, closes CEF nonblocking, and releases native references
- Reopen keeps the logical ID and handler registry, but gets a fresh native
  browser, endpoint, token and monotonically new generation
- Dequeued work is rechecked against its generation before execution; a handler
  that closes/reopens the view cannot deliver its old result to the replacement
- Pre-exit uses `OnEnginePreExit` while engine/Slate services still exist;
  module shutdown also removes delegate registrations idempotently
- No `AsyncTask` closure is left in the task graph after module shutdown
- Dynamic reload is disabled because a live UCLASS/CEF DLL unload is not proven

## Security and scope

Input HTML is trusted plugin-owned content, not arbitrary pages. Each initial
document has a unique subdomain under `.auroraview.invalid`, separate from the
endpoint token. This does not prove CEF browser-context/storage isolation. Navigation
accepts only one load of the exact document URL, forbids frames and redirects,
and suppresses popups. An early CSP blocks networking, external scripts, objects,
forms and frames. These are defense-in-depth boundaries, not a claim to sandbox
hostile same-document JavaScript. The exposed native methods only enqueue
bounded JSON; host APIs must be registered explicitly. No Python execution,
shell command, filesystem or arbitrary UObject introspection endpoint is exposed.

## Core reuse boundary

The source audit found no stable C ABI to reuse for the Rust backend in this
minimum target. Adding a competing window engine would duplicate Core and force
an unverified ABI into the Editor. We instead reuse its JS bridge exactly and
adapt the engine-supported rendering and thread-dispatch surfaces. The adapter
does not carry Python lifecycle code or modify the public Core checkout.

Pinned sources:

- [Core repository at audited commit](https://github.com/try-auroraview/auroraview/tree/11b3a29ad95a46cb22aaa604614de16da16bfc22)
- [Core event bridge source](https://github.com/try-auroraview/auroraview/blob/11b3a29ad95a46cb22aaa604614de16da16bfc22/packages/auroraview-sdk/src/inject/event_bridge.ts)
- [Existing host reply envelope](https://github.com/try-auroraview/auroraview/blob/11b3a29ad95a46cb22aaa604614de16da16bfc22/python/auroraview/core/mixins/api.py)
- [MIT license](https://github.com/try-auroraview/auroraview/blob/11b3a29ad95a46cb22aaa604614de16da16bfc22/LICENSE)

## Official Unreal sources checked 2026-10-05

- [SWebBrowser API](https://dev.epicgames.com/documentation/en-us/unreal-engine/API/Runtime/WebBrowser/SWebBrowser): supports a provided IWebBrowserWindow, LoadString, script execution and binding
- [UE 5.6 BindUObject](https://dev.epicgames.com/documentation/en-us/unreal-engine/API/Runtime/WebBrowser/SWebBrowser/BindUObject?application_version=5.6): lowercase JS names; asynchronous returned futures; permanent binding ownership
- [FCreateBrowserWindowSettings](https://dev.epicgames.com/documentation/en-us/unreal-engine/API/Runtime/WebBrowser/FCreateBrowserWindowSettings) and [CreateBrowserWindow](https://dev.epicgames.com/documentation/en-us/unreal-engine/API/Runtime/WebBrowser/IWebBrowserSingleton/CreateBrowserWindow): native factory can return null
- [CloseBrowser](https://dev.epicgames.com/documentation/en-us/unreal-engine/API/Runtime/WebBrowser/IWebBrowserWindow/CloseBrowser): force and blocking flags
- [FWebNavigationRequest](https://dev.epicgames.com/documentation/en-us/unreal-engine/API/Runtime/WebBrowser/FWebNavigationRequest): main-frame and redirect information
- [FTSTicker](https://dev.epicgames.com/documentation/unreal-engine/API/Runtime/Core/FTSTicker): ticker ownership/removal API
- [FCoreDelegates](https://dev.epicgames.com/documentation/unreal-engine/API/Runtime/Core/FCoreDelegates): OnEnginePreExit occurs before core-module shutdown
- [UE 5.6 plugins](https://dev.epicgames.com/documentation/en-us/unreal-engine/plugins-in-unreal-engine?application_version=5.6): source-module layout, project plugin location and rebuild requirements
- [UE 5.6 toolchain](https://dev.epicgames.com/documentation/en-us/unreal-engine/setting-up-visual-studio-development-environment-for-cplusplus-projects-in-unreal-engine?application_version=5.6): use the installed engine's supported VS/MSVC/SDK selection

Some unversioned API pages currently render newer documentation. The versioned
SWebBrowser overview could not be fetched, while the versioned 5.6 binding page
was available. This is API-design evidence, not proof that every signature in
the historical candidate compiles on 5.6, or the current experimental candidate
compiles on 5.7. Exact installed headers and UHT/UBT remain the
authority. Epic code was not copied into this candidate.

## Interface coordination for shared Core owner

1. Keep `window.ipc.postMessage` as a supported transport injection point or
   expose a documented setter; UE already uses that entry point unchanged
2. Preserve `__auroraview_call_result` and error fields across host integrations
3. For future nonblocking host jobs, define completion/cancellation/timeout
   ownership once in Core before adding native async callback support
4. Export a versioned bridge asset manifest for non-Python embedders; this local
   candidate pins existing assets by commit and hash until then
5. Do not let a published support table treat this source candidate as validated
