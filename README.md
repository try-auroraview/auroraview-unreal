# AuroraView Unreal

Experimental native AuroraView integration for Win64 **Unreal Editor and packaged games**. The source/build matrix matches dcc-mcp-unreal: **4.18, 4.26, 5.5, 5.7 and 5.8**. Each engine requires its own build and acceptance receipts; admission to the matrix alone does not certify compatibility.

The Runtime module owns WebBrowser/CEF views, the unchanged AuroraView Core `call` / `invoke` / result / event bridge, GameThread dispatch and optional authenticated loopback communication. The Editor module adds native docking, inspector and drag/drop integrations. Existing per-view C++ APIs remain available through the Editor facade.

External Python scripts can register tools, receive browser calls, call native Unreal tools and exchange events in both Editor and games. No embedded Python dependency is needed. Where the engine provides and enables PythonScriptPlugin, Editor additionally offers `unreal.python.execute`. UE 4.18 and packaged games use external Python and native/project tools.

```python
from auroraview_unreal import Client

with Client(port=18770, token=token, expected_pid=pid,
            expected_engine="5.7.", expected_context="game") as host:
    host.bind_call("tools.multiply", lambda a, b: a * b)
    print(host.call("unreal.engine.info"))
    host.emit("tools:status", {"ready": True})
```

Install the stdlib-only client with `uv pip install ./python`, or use `PYTHONPATH=python`. See [Python and native tools](docs/python-tools.md) and [the script example](examples/python_tools.py).

Start an owned host with `-AuroraViewHostPort=<port> -AuroraViewHostToken=<32+ character token>`. Native reflection, console execution and shutdown additionally require `-AuroraViewAllowControl`. Keep tokens private; the listener binds only `127.0.0.1`. The plugin must be enabled in the target Editor project or compiled into the game.

The shared control surface discovers loaded worlds, actors, objects, properties and reflected UFunctions. Project tools extend it with nonblocking operations and explicit application semantics. Editor APIs, latent UFunctions and arbitrary native functions do not become Runtime APIs automatically.

## Build and validate

[Local builds and CI](docs/building.md) use the same scripts. A complete validation has independent gates:

1. Portable C++, JavaScript and Python contracts: `pwsh -File scripts/check.ps1` or `bash scripts/check.sh`.
2. Actual UHT/UBT Editor and Development/Shipping Game compilation plus package integrity: `scripts/build_plugin.py`.
3. Nine real graphical Editor Automation tests: `scripts/validate_editor.ps1`.
4. Actual BuildCookRun, staged Game identity, native reflection, external Python tools, events and normal process exit: `scripts/validate_game.ps1`.

The Game validator runs headlessly with NullRHI, except UE 5.5 uses offscreen D3D11 to avoid an engine Nanite shutdown fault. Rendered Game CEF acceptance is a separate gate. Manual docking/drag destination behavior also remains separate. See [version policy](docs/version-validation.md), [architecture](docs/architecture.md), [acceptance history](docs/acceptance.md) and [native showcase](docs/native-showcase.md).

The original AuroraView Core assets stay byte-for-byte pinned to `11b3a29ad95a46cb22aaa604614de16da16bfc22`, with MIT notice and SHA-256 manifest. UE4's Chromium 59 uses reproducibly generated legacy bundles; UE5 uses the original assets. The engine supplies its own CEF binaries. DLLs cannot be shared across engine minors, BuildIds or architectures. Dynamic module reload remains disabled.

[Upstream integration and splitting](docs/upstream-integration.md) records the existing AuroraView contract/adapter extraction and the remaining concrete factory and UE integration work. This repository keeps Unreal lifecycle, native widgets, thread affinity and packaging in the Unreal adapter.
