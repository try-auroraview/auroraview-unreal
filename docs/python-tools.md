# Python tools and native Unreal control

The stdlib-only `auroraview_unreal` client supports Python 3.9+ independently of Unreal's embedded Python. Install with `uv pip install ./python`, or add `python/` to PYTHONPATH. See [the complete example](../examples/python_tools.py).

Enable AuroraView in the target project/game, then supply an unused port and private token with at least 32 characters:

```text
-AuroraViewHostPort=<port> -AuroraViewHostToken=<token> -AuroraViewAllowControl
```

The listener is opt-in, binds loopback only and authenticates every connection. `AuroraViewAllowControl` separately enables native reflection, console and shutdown; tool communication does not require that broad control flag. Use expected PID, engine prefix and context to reject the wrong host. Tokens are trusted local credentials, not a sandbox for untrusted scripts.

```python
from auroraview_unreal import Client

with Client(port, token, expected_pid=pid, expected_engine="5.7.",
            expected_context="editor") as host:
    host.bind_call("tools.multiply", lambda a, b: a * b)
    print(host.call("unreal.engine.info"))
    host.emit("tools:ready", {"ready": True})
```

The browser uses the existing Core interface:

```javascript
await window.auroraview.whenReady();
const value = await window.auroraview.call('tools.multiply', {a: 6, b: 7});
window.auroraview.send_event('tools:result', {value});
```

`bind_call` maps absent params to no arguments, objects to keyword arguments, arrays to positional arguments and scalar values to one argument. `bind_api` exports public callable methods with a namespace. Tool registration is connection-owned, cannot overwrite another owner's tool, and reserves `unreal.*` and `auroraview.*`. Disconnect unregisters tools and fails pending calls. SDK handlers/events run on bounded workers, separate from the socket reader, and can call the host synchronously. Callbacks must return cooperatively; stdlib Python cannot forcibly stop a hung user thread. `call_async` returns a Future; timeout/cancellation stops waiting, not an already-running Unreal operation.

Future done callbacks run on the completing thread and must remain nonblocking. Use `call_async` without waiting there, or use SDK event/tool callbacks for synchronous host calls. A synchronous call from the socket reader fails immediately with a clear error. Calling `close()` inside a callback signals shutdown; an external `close()` joins the threads and verifies completion.

| Method | Parameters and behavior |
| --- | --- |
| `unreal.engine.info` | Actual PID, engine version, Editor/Game context and enabled capabilities; available without broad control |
| `unreal.world.list` | Loaded world paths and types |
| `unreal.actor.list` | Optional `world` path; selects a single unambiguous live world; bounded to 4096 actors |
| `unreal.object.describe` | Full loaded `object` path; reflected properties, functions and parameter types |
| `unreal.object.get` | `object`, `property` |
| `unreal.object.set` | `object`, `property`, JSON `value`; typed conversion and Editor change notifications |
| `unreal.object.call` | `object`, `function`, object `args`; JSON conversion, ProcessEvent, `return_value` and `out` |
| `unreal.console.execute` | `world`, `command`; reports whether the engine handled it |
| `unreal.python.execute` | `code`; requires `-AuroraViewAllowControl`; available only in Editor with installed/enabled PythonScriptPlugin |
| `auroraview.host.describe` | Protocol and actual host identity/capabilities |
| `auroraview.tools.list` | Native and currently registered project/Python tools |
| `auroraview.view.open` | `id`, trusted local `html`, optional `title`; owns an independent native browser window |
| `auroraview.view.describe` | `id`; readiness and current generation |
| `auroraview.view.show/hide/close/remove` | `id`; lifecycle operations |
| `auroraview.host.shutdown` | Graceful owned-host process exit after flushing the response |

Object addresses resolve already-loaded full paths. Property and function arguments use Unreal's typed JSON conversion and reference resolution rules. Function input names are exact reflected names, all required inputs must be supplied, and unknown arguments fail before invocation. Latent functions require a project tool with explicit asynchronous completion ownership. Native tools execute on GameThread and must not block. Project-specific C++ tools register with `FAuroraViewRuntimeModule::RegisterTool`; per-view `BindCall` remains supported.

Editor Python can reach the available `unreal` Python API. UE4.18 has no PythonScriptPlugin, and packaged games do not provide Editor Python. External Python calls the common native tools or project/Blueprint UFunctions in both contexts. The plugin must be present in the target; it does not attach to unmodified games or bypass Editor-only API boundaries.

The transport reuses AuroraView parent IPC v1: UTF-8 newline-delimited JSON with 1 MiB frames. An authenticated `hello`/`hello_ack` negotiates `rpc_protocol: "auroraview.unreal/1"`. Core call envelopes travel in `__auroraview_rpc` events; unchanged Core results travel in `__auroraview_call_result` events. Ordinary transport `data` maps to browser `detail`. The SDK requires explicit acceptance and capability/identity checks without legacy fallback. See [architecture](architecture.md) for ownership and limits.
