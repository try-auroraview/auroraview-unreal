# Demo architecture and extension points

The demo is a sample consumer of AuroraView's Unreal adapter. The reusable plugin owns the native browser, bridge, host communication and Unreal lifecycle. The sample owns its scene, reflected actions, dashboard content and Python tools.

```text
AuroraView Core call / invoke / result / event contracts
                          |
                AuroraViewRuntime adapter
                 /                    \
       thin Editor integration     packaged Game host
                 \                    /
                  native sample module
                          |
         authenticated loopback tool/event connection
                          |
               external Python tool provider
```

| Layer | Responsibility | Extension point |
| --- | --- | --- |
| Pinned AuroraView Core | Existing JavaScript promises, call/invoke envelopes, result and event semantics | Consume the pinned assets and conformance contract |
| `AuroraViewRuntime` | CEF/Slate view ownership, GameThread dispatch, connection identity, bounded tool routing and reflection | Native `RegisterTool`, per-view `BindCall`, shared host tools |
| `AuroraViewEditor` | Editor startup, menus, docking and workspace policy | Editor-specific presentation and tools |
| Sample module `AuroraViewGameFixture` | Real two-cube scene, floor, camera, lighting, `SetCubeHeight` and `GetDemoState` | Replace with project/Blueprint actions and explicit readback |
| `Resources/live_demo.html` | Host identity display, buttons and observed results | Replace trusted HTML/CSS while retaining Core calls and events |
| External Python process | Multiply tool and event round trip; connection lifecycle | Register project namespaces with `Client.bind_call` or `bind_api` |
| `scripts/run_demo.py` | Build/preparation, verified package/bundle reuse and owned-host startup/shutdown | Keep launch policy outside the runtime plugin |

The sample's Game target depends on Runtime. Editor-only modules and embedded Python are not required in the cooked game. The Editor path adds the thin Editor integration around the same Runtime bridge and native scene actions.

The Editor launcher requests `presentation: "docked"` through `auroraview.view.open`, then calls the Editor-only `editor.view.dock` tool to attach it beside the scene in the Level Editor. Runtime owns the native `SDockTab` and browser lifecycle, while Slate owns dragging and layout attachment. This demo attachment uses a temporary document tab; it does not persist a new layout into the user's Editor configuration. `auroraview.view.describe` reports actual native presentation state, the stable registered tab type and its current layout identifier. Game mode uses `presentation: "floating"`; a floating Slate window is a separate presentation from a viewport or UMG embed.

## Follow a scene action

1. The dashboard uses AuroraView Core to call the external Python `demo.scene.set_height` tool.
2. Python asks the authenticated host to invoke the sample's reflected `SetCubeHeight` function.
3. Runtime validates the object and arguments, then executes the native action on Unreal's GameThread.
4. Python requests `GetDemoState`; native state readback returns to the dashboard as a result and scene event.

Reflection resolves loaded full object paths, validates argument names and converts values using Unreal's reflected types. It does not expose arbitrary native functions or automatically support latent UFunctions. A project-specific tool should own asynchronous completion, cancellation and domain validation when reflection is insufficient.

## Follow a Python call or event

Python runs in its own process. The client authenticates the loopback connection and binds it to the expected native PID, engine and Editor/Game context. The browser's multiply call reaches a connection-owned Python tool, whose result returns through the unchanged Core result bridge. `invoke` follows Core's separate invoke result semantics.

The event example sends a browser nonce to Python and returns it with `false` and `0`. Checking the exact returned payload establishes both direction and payload preservation. A successful connection or browser-ready notification alone does not establish that round trip.

The listener is opt-in and loopback-only. Its private token grants trusted local control, and the launcher enables the native control flag for this owned sample. Tokens are not public demo configuration or a sandbox for untrusted HTML. Closing the Python provider unregisters its tools; the host reports unavailable tools instead of inventing results.

## Shared DCC-MCP backend boundary

The optional shared consumer uses the public [`auroraview-dcc-mcp` 0.1.0 preview](https://github.com/try-auroraview/auroraview/releases/tag/auroraview-dcc-mcp-v0.1.0-preview.1). `Tool` declares schemas, annotations and synchronous business handlers; `ToolSet.borrow()` supplies tool listing, calls and removable subscriptions. `NativeToolBinding` publishes those declarations through the existing Unreal parent-IPC connection. The default demo retains its standard-library-only provider.

Shared tool calls and event delivery run on the thread that created the `ToolSet`. Native SDK callbacks arrive on bounded workers, so `--shared-tools` dispatches them through the launcher's existing main loop. It creates no additional thread, event loop, server or registry. Closing the binding revokes queued work, unregisters only its own native tools and releases its borrowed session. It does not close the `ToolSet`, client or a borrowed server. The demo closes its own `ToolSet` separately when the host session ends.

`tools.attach(existing_server)` requires an actual same-process DCC-MCP Core server. An external Python process cannot attach to the Editor's Python server object. Embedded Editor handlers must call Unreal APIs directly on GameThread; synchronously calling the same process's native control endpoint there would deadlock. Unreal continues to own Slate/CEF, native objects and GameThread scheduling. Existing pinned browser assets are unchanged by this Python integration. Shared Python contract tests, live MCP discovery and native browser/docking acceptance are separate evidence gates.

## Adapt the demo

- Replace the sample scene and reflected functions in your own native module or Blueprint objects. Keep native state readback close to the action being verified.
- Put portable business logic and external service access in Python tools using a project namespace. Keep handlers bounded and cooperative.
- Use native tools for operations that must execute on GameThread. Avoid waiting there for Python or network work.
- Customize `live_demo.html` for the user workflow. Trusted dashboard content consumes the shared bridge; it does not need a second IPC protocol.
- Add Editor presentation or selection policy to the Editor module. Keep packaged Game dependencies free of `UnrealEd` and Editor Python.

The [full Runtime architecture](architecture.md) describes ownership, teardown, wire envelopes and limits. The [upstream integration plan](upstream-integration.md) identifies shared contract/assets, backend factory and portable Python extraction opportunities for other DCC adapters. Unreal lifecycle, native widget ownership, GameThread scheduling and engine packaging stay in the host adapter.
