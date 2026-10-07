# Shared AuroraView integration and extraction

This record audits
[`try-auroraview/auroraview` at `cde69c8`](https://github.com/try-auroraview/auroraview/tree/cde69c8c5c185682fd0ed3f9adaca57be10d3743)
on 2026-10-08. It distinguishes merged abstractions from functioning native
backends. The separately pinned JavaScript asset revision remains recorded in
`ThirdParty/AuroraViewCore/manifest.json`.

## Existing upstream contracts

The upstream already established the dependency direction needed by individual
DCC integrations:

```text
auroraview-contract <- auroraview-core <- host adapters
```

[`crates/auroraview-contract`](https://github.com/try-auroraview/auroraview/tree/cde69c8c5c185682fd0ed3f9adaca57be10d3743/crates/auroraview-contract)
is a zero-dependency leaf, independently versioned at `1.0.0`. Its additive
version policy and non-exhaustive public enums allow capability and host
extensions. Core reexports the contract as `auroraview_core::contract`, keeping
Rust adapters on the same contract types as their Core dependency.

| Existing seam | Authoritative upstream files | Purpose |
|---|---|---|
| Host identity and discovery | `crates/auroraview-contract/src/host.rs`; `python/auroraview/adapter/base.py`, `hosts.py`, `registry.py` | Lazy registration, detection, version, UI framework, thread model and embedding metadata |
| Render selection | `crates/auroraview-contract/src/backend.rs`; `python/auroraview/adapter/backends.py` | `RenderBackend`, `RenderSurface`, `SurfaceSpec`, priority registry and backend families |
| Capability reporting | `crates/auroraview-contract/src/capability.rs`; `python/auroraview/adapter/capability.py` | Supported, unsupported with remediation, or runtime-dependent unknown |
| Host thread dispatch | `python/auroraview/utils/thread_dispatcher/` | Existing lazy dispatcher registry; adapters delegate rather than duplicate it |
| Browser RPC/events | `packages/auroraview-sdk/src/core/types.ts`, `src/inject/event_bridge.ts`; `python/auroraview/core/mixins/api.py` | Existing call/invoke envelopes, result events and Python binding semantics |
| Parent/child transport | `crates/auroraview-core/src/parent_ipc/{protocol,bridge,context}.rs` | Host-independent loopback NDJSON framing, handshake and named events |

The design record is
[`docs/design/adapter-contract.md`](https://github.com/try-auroraview/auroraview/blob/cde69c8c5c185682fd0ed3f9adaca57be10d3743/docs/design/adapter-contract.md),
with follow-up work in
[RFC 0019](https://github.com/try-auroraview/auroraview/blob/cde69c8c5c185682fd0ed3f9adaca57be10d3743/docs/rfcs/0019-pluggable-backend-and-host-adapter-contract.md).
New adapters should extend these seams instead of introducing another host
enum, dispatch registry or call/result protocol.

## Gaps in the audited revision

1. **Selection does not yet create a real registered surface.**
   `crates/auroraview-core/src/backend/factory.rs` still uses a closed
   `BackendType` match. The Windows Wry and WebView2 branches both create
   `WryBackend`. Contract backend descriptors can announce availability while
   `create_surface` still returns unsupported. The upstream design explicitly
   records this gap; a selected descriptor is not proof of a usable backend.
2. **Host metadata is not native integration.**
   `python/auroraview/adapter/hosts.py` describes Unreal as Slate/GameThread and
   delegates thread dispatch. It does not install a native Unreal plugin or
   implement a Slate surface factory. `crates/auroraview-dcc/src/config.rs`
   still contains the closed `DccType` host enum.
3. **The Rust UE crate remains a prototype.**
   `crates/auroraview-ue/src/lib.rs` returns a null `SlateWidgetHandle` from
   `create_webview`; its Blueprint node structures are placeholders. Its tests
   establish Rust data/executor behavior, not a working native widget,
   GameThread binding or packaged Unreal integration.
4. **Thread ownership needs host evidence.**
   `python/auroraview/utils/thread_dispatcher/backends/unreal.py` registers Slate
   callbacks and assumes callback return values provide one-shot removal.
   Native callback lifecycle, timeout and shutdown behavior must be verified in
   Unreal before this is used as the ownership model for another adapter.

This repository implements its actual Unreal lifecycle through native modules.
It does not advertise the Rust prototype, a Chromium descriptor, or a Python
metadata adapter as that implementation.

## Smallest next extraction

| Step | Upstream change | Adapter responsibility and acceptance |
|---|---|---|
| 1. Publish the shared wire/asset contract | Export the bridge assets, versioned manifest and call/result/event conformance fixtures as a reusable distribution. Document parent IPC's named-event extension mechanism. | Consume pinned hashes and run the same fixtures against the native CEF transport. Keep the Unreal bootstrap small. |
| 2. Wire the existing backend registry | Register an actual surface factory together with availability; route `BackendFactory` through that registry. Preserve deprecated enum shims during migration. | Prove that a selected backend can create and close a surface, or provide a structured unsupported answer before selection. |
| 3. Separate the portable Python tool layer | Extract reusable connection/call/event/tool registration behavior without importing Qt, a DCC SDK or a native extension at import time. Keep host identity and negotiated extensions explicit. | Retain Unreal launch flags, native method names and engine identity checks in `auroraview_unreal`; reuse a shared client only after conformance tests cover both. |
| 4. Move host implementations behind registration | Move each host's discovery, dispatch and embedding implementation to its adapter package. Let compatibility shims delegate to the registry. | Qt DCCs may share proven Qt lifecycle utilities. Unreal keeps C++/Slate and GameThread ownership; other native hosts keep their own lifecycle rules. |

The first useful extraction is a consumable contract and asset distribution,
followed by real factory wiring. Moving placeholder implementations into more
repositories would not establish these boundaries. Physical repository moves
and releases can follow after the consumers and compatibility shims work.

## Unreal's additive transport extension

The native server and external Python client use upstream parent IPC v1
framing. Ordinary events keep `{type:"event",event,data}`. The negotiated
`auroraview.unreal/1` extension adds `rpc` and `tools` capabilities and carries
the unchanged Core call envelope inside the `__auroraview_rpc` named event.
Results use the existing `__auroraview_call_result` envelope/event. The
authentication token is an application field in `hello.data`; it is not a
claimed upstream authentication feature.

That extension should be proposed upstream as a reusable, tested application
of named events. It should not be presented as a replacement for the existing
browser protocol. The browser-side `invoke` path retains Core's
`__invoke_result__` event. Parent events carry `data`, while browser events
carry `detail`; adapters perform this mapping explicitly.

The Unreal client requires an accepted, identity-bearing acknowledgement and
does not downgrade an authorized control connection to legacy mode. Other
applications may still use upstream's documented legacy fallback for their own
non-control channels.

## Integration criteria for additional DCC adapters

- Importing the portable contract or client does not initialize a DCC, Qt or a
  browser engine.
- The host owns its UI/GameThread/STA dispatch and destruction. Shared code does
  not make apartment-bound objects safe by placing them behind a mutex.
- Registry selection and capability reporting reflect the linked implementation
  and current host context. Transport features do not imply CDP or rendering
  features.
- Shared tests cover call/result compatibility, falsy event payloads, malformed
  frames, bounded queues, timeouts, disconnection and provider ownership.
- Each adapter supplies its own native creation/teardown evidence, packaging
  verification and supported-version results. A portable unit test is not
  evidence that a native host works.

The [Runtime architecture](architecture.md) defines this repository's current
module and project-tool boundaries. Editor features remain optional native
policy; the same portable tool provider can communicate with a verified Editor
or a packaged Game without requiring their Python environments to match.
