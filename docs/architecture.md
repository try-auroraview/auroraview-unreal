# Runtime host and shared contracts

AuroraView Unreal uses the same AuroraView JavaScript call, result and event
contracts in Editor views and packaged games. Unreal owns rendering, native
objects and its GameThread. An external Python process can provide tools without
loading a Python interpreter into the game.

Module boundaries describe the implementation; they do not certify every
engine version. See [build validation](building.md) and
[version evidence](version-validation.md) for the separate build and execution
gates. The [upstream integration plan](upstream-integration.md) records what is
shared today and what still needs extraction.

## Dependency direction

```text
AuroraView Core JavaScript assets and wire contracts
                       |
              AuroraViewRuntime
              /               \
     AuroraViewEditor     Project Runtime tools
              \               /
                Unreal host
                       |
          external Python tool providers
```

| Boundary | Owns | Does not require |
|---|---|---|
| `Source/AuroraViewRuntime` | CEF/Slate presentations, per-view sessions, the Core bridge endpoint, shared tool routing, native reflection and loopback transport | `UnrealEd`, an embedded Python interpreter, or the Editor module |
| `Source/AuroraViewEditor` | Editor startup/menu policy, workspace layout, selection and asset/actor tools, fixture preparation and Editor automation | Ownership of a second browser engine or transport |
| `python/auroraview_unreal` | External connection lifecycle, Core calls/results, Python tool registration and bounded callback workers | Unreal's Python module, Qt or a native Python extension |
| Project tools | Project-specific actions, validation and completion semantics | Changes to AuroraView's core protocol |

The Editor facade delegates presentation operations to Runtime, preserving
existing C++ callers. Generic Slate presentation code stays in Runtime; Editor
workspace and selection policy stays in Editor. A packaged Game target depends
on `AuroraViewRuntime` and excludes `AuroraViewEditor`.

## Core bridge in a native view

The initialization path is:

```text
pinned bridge stub -> Unreal compatibility bootstrap -> trusted HTML fragment
-> bound UObject transport -> pinned Core event bridge
```

The existing Core request envelopes are preserved:

```json
{"type":"call","id":"request-id","method":"project.echo","params":{"value":1}}
{"type":"invoke","id":"request-id","cmd":"project.echo","args":{"value":1}}
{"type":"event","event":"project:changed","detail":{"value":1}}
```

Results retain `{id,ok,result}` or `{id,ok:false,error:{name,message,code}}`.
Calls return through `auroraview.trigger('__auroraview_call_result', result)`;
plugin-style invokes use Core's `__invoke_result__` event. Event delivery uses
`auroraview.trigger()` throughout. This is AuroraView's existing protocol, with
its existing promises and request IDs.

`BindUObject` queue admission is asynchronous. Its boolean acknowledgement
means that a request entered the mailbox; the result arrives separately after
the handler runs. Core's ready event has a separate, token-checked lifecycle
slot so a full RPC queue cannot silently discard initialization.

Per-view handlers registered through `BindCall` take precedence for that view.
Other calls reach the shared native/Python tool router. Unknown methods return
a structured error rather than reporting success.

## External Python transport

The native host reuses upstream **parent IPC v1**: a loopback TCP server,
UTF-8 newline-delimited JSON, and `hello` / `hello_ack` / `event` / `ping` /
`pong` frames. Receivers tolerate CRLF and a UTF-8 BOM; frames are bounded to
1 MiB. Ordinary parent IPC events use `data`; the browser bridge maps that
payload to Core's `detail`.

This repository adds the negotiated `auroraview.unreal/1` extension. It does not
replace parent IPC or invent a second call envelope:

```json
{"type":"event","event":"__auroraview_rpc","data":{"type":"call","id":"request-id","method":"project.echo","params":{"value":1}}}
{"type":"event","event":"__auroraview_call_result","data":{"id":"request-id","ok":true,"result":{"value":1}}}
```

The same extension carries calls to external Python providers and their
results. Ordinary application events retain parent IPC's `{type,event,data}`
shape and fan out to connected clients and live views.

The listener starts only with an explicit host port and a token of at least
32 characters. The client includes that token in `hello.data`, together with
the requested capabilities. An accepted acknowledgement identifies the native
PID, engine version, `editor` or `game` context, and the `event`, `rpc` and
`tools` capabilities. The SDK can require that identity before sending work.
This control connection requires an explicit accepted acknowledgement; it does
not use upstream's optional legacy-handshake fallback.

These handshake capabilities describe transport and tool routing. They do not
claim CDP, transparency, browser-cookie support or rendering acceptance. A
generic upstream `ParentBridge` needs the application's authentication and RPC
negotiation to connect to this host.

## Tools and Unreal control

Native project modules register bounded synchronous handlers with
`RegisterTool`. External Python providers use `Client.bind_call` or
`Client.bind_api`; registration publishes a method description and parameter
schema under the provider's connection. A disconnected provider loses its
registrations and pending calls fail explicitly. The `auroraview.` and
`unreal.` namespaces belong to the host; project tools use their own names.

Native handlers execute on GameThread. The transport polls nonblocking sockets
and routes reverse calls without waiting for Python on that thread. Python
callbacks use bounded workers and can make further host calls. A call deadline
ends the wait; it does not forcibly cancel a Python function already running.
Tool providers must implement cooperative cancellation for longer work.

`unreal.engine.info` supplies identity and capability readback. The explicit
`-AuroraViewAllowControl` switch enables the native world/actor queries,
console execution, and reflected property/UFunction operations on loaded
objects. Reflection validates the object path and parameter conversion before
calling `ProcessEvent`. Latent UFunctions require a project tool with explicit
completion ownership. A console reply reports whether Unreal handled the
command; callers should read back the state required by their own operation.

`unreal.python.execute` is available only when the Editor's Python plugin is
loaded. Packaged games use external Python and native/project tools. The plugin
must already be installed and loaded in the target host; this API does not
attach to an unmodified Unreal process or make Editor-only APIs available in a
game.

## Ownership and shutdown

- Runtime owns the session registry, removable GameThread ticker, shared tools
  and control listener.
- A view owns its Slate/browser objects, strong UObject endpoint and independent
  mailbox. Browser delegates capture mailbox and generation, not raw host objects.
- Closing invalidates admission and queued work before native teardown. Reopening
  preserves the logical ID and handlers but creates a fresh browser, endpoint,
  token and presentation generation.
- Replies recheck presentation generation. A handler that closes or reopens a
  view cannot deliver its old result to the replacement.
- Dock teardown waits for Slate to finish adopting a returned tab. Reuse accepts
  only the current session's tab, including after a failed open or layout restore.
- Pre-exit retires host work while engine/Slate services remain available; module
  shutdown removes registrations idempotently. Dynamic reload is disabled for
  live UCLASS/CEF ownership.
- `auroraview.host.shutdown` acknowledges the authorized request and requests
  normal engine exit after output draining. Validation separately requires a
  zero process exit; forced cleanup never establishes a passing run.

HTML fragments are trusted tool content. Document URLs, endpoint tokens,
navigation rules and the early CSP constrain the owned view; they are not a
sandbox for hostile same-document JavaScript. The optional control token grants
access to registered tools in that host and should be treated accordingly.

## Shared assets

`ThirdParty/AuroraViewCore/manifest.json` records the exact upstream commit,
asset paths and SHA-256 hashes. That asset revision is independent of a newer
architecture audit. Build and validation receipts verify the packaged assets
against the manifest. The Unreal adapter supplies engine-specific rendering
and dispatch; it does not require the upstream Rust WebView2/Qt backend to own a
second window or event loop.
