# Run the AuroraView Unreal demo

This demo combines a real Unreal scene, a native CEF/Slate dashboard and an external Python tool provider. Editor mode uses a native `SDockTab`, which can be attached to the Editor layout. The cooked Development game uses a separate native Slate browser window. The same Core/Python tools work in both contexts. The [native showcase](native-showcase.md) covers additional Editor workspace and drag/drop work.

## Run from source

Use Windows x64, Python 3.9+, a supported installed Unreal engine, and the Visual Studio/Windows SDK required by that engine. Unreal is not downloaded or installed by the launcher. Run these commands from the repository checkout.

For Editor:

```powershell
python scripts/run_demo.py --engine-root "C:\Program Files\Epic Games\UE_5.7" --mode editor --output "C:\AuroraViewDemo\Editor"
```

For a cooked game:

```powershell
python scripts/run_demo.py --engine-root "C:\Program Files\Epic Games\UE_5.7" --mode game --output "C:\AuroraViewDemo\Game"
```

Replace the engine path with your actual installation. Use a fresh output directory outside the repository and engine. Building and cooking can take several minutes. Keep the launcher terminal open: it owns the external Python tool provider and the process it launches.

The launcher builds the plugin, prepares the isolated sample project and starts the selected host. Game mode compiles and cooks the native sample; it does not run an HTML mock or rely on an installed Editor process. To stop, press Ctrl+C in the launcher terminal or close the owned host normally.

## Try the dashboard

Wait for Unreal and the dashboard to report their actual identity and readiness. A normal run has a scene with two colored cubes, a floor, a camera and lighting.

| Action | Observable result |
| --- | --- |
| Inspect host | Actual Unreal version, Editor/Game context and PID; Python tool-provider status |
| Python multiply | AuroraView Core `call` and `invoke` reach external Python; `6 × 7` returns `42` |
| Lift cube | A reflected native scene function changes the cube's height; native state readback confirms the value |
| Reset scene | The cube returns to its initial height; state readback confirms the reset |
| Event round trip | Browser sends a nonce to Python; the returned event preserves that nonce, `false` and `0` |

The rendered scene and dashboard are independent views of the same host. If an operation reports an error, use the returned message and launcher evidence; a button click alone is not proof that Unreal changed.

In the Editor demo, close the dashboard tab and use **Window → Reopen AuroraView Demo** to open it in the default Editor stack again. Keep the Python launcher running. This command reuses the registered view and its tools; it does not restore the exact position of a user-dragged layout. The generic registered Tab entry and the demo's explicit reopen command have different layout policies.

## Reuse a verified build

An existing plugin package can avoid a second plugin build:

```powershell
python scripts/run_demo.py --engine-root "C:\Program Files\Epic Games\UE_5.7" --mode game --package "C:\AuroraViewBuild\Package" --output "C:\AuroraViewDemo\Game"
```

The launcher checks package integrity and engine identity before reuse. Copy the complete verified package and its required provenance together; DLLs from another engine minor, BuildId or architecture are incompatible.

To reopen the same prepared output, pass `--reuse` with the original command. Reuse requires matching engine, source, sample-template and package identities. Each Editor session receives a verified copy of the prepared project, so Editor configuration writes do not change the reusable build inputs. After changing those inputs, use a new output directory. The launcher does not replace an unrelated existing directory.

Use `--prepare-only` to build and prepare without opening Unreal. For a bounded scripted demonstration, use `--session-seconds 60`; the launcher requests normal shutdown after that interval. An ordinary interactive launch stays open until Ctrl+C or host closure.

## Run an offline game bundle

A complete offline bundle includes the cooked game, its native browser resources, this launcher and the stdlib-only Python client. It needs Windows x64, Python 3.9+ and the Microsoft Visual C++ x64 runtime compatible with the compiler used for the game. The bundle includes a signed Microsoft runtime installer; follow its README if that runtime is missing or older. Unreal and Visual Studio are not required on the receiving machine.

```powershell
python scripts/run_demo.py --bundle "C:\AuroraViewDemoBundle" --output "C:\AuroraViewDemo\OfflineRun"
```

Keep the bundle intact when copying it. The launcher verifies its manifest and uses a fresh run directory. Downloadable bundle and real media links are recorded below after publication and readback; no download is claimed by the source instructions alone.

## Validation and media

The previously merged integration compiled Editor, Development and Shipping targets for UE 4.18, 4.26, 5.5, 5.7 and 5.8. Its Runtime and rendered-browser evidence belongs to the source and engine identities recorded in those receipts. This new visible sample needs its own results.

| Version | Visible Editor sample | Visible packaged Development sample | Media / downloadable bundle |
| --- | --- | --- | --- |
| 4.18 | Pending native validation | Pending native validation | Pending |
| 4.26 | Pending native validation | Pending native validation | Pending |
| 5.5 | Pending native validation | Pending native validation | Pending |
| 5.7 | Pending native validation | Pending native validation | Pending |
| 5.8 | Pending native validation | Pending native validation | Pending |

Screenshots and recordings will be added only from real host runs, with engine/context and source identity. Public media will live under `docs/media/live-demo/` or a verified release asset. There are no generated or placeholder screenshots in this guide.

## Limits and troubleshooting

- The current matrix is Win64. Other platforms and unlisted Unreal versions need their own native validation.
- The launched game is Development. Shipping compilation is a separate check; it does not establish Shipping runtime or browser acceptance.
- Editor docking and Game presentation are separate capabilities. The Game demo opens a native Slate window; it does not demonstrate a UMG widget embedded in the game viewport.
- The plugin must be enabled in an Editor project or compiled into a game. It does not attach to an unmodified game.
- External Python works in both contexts. Embedded `unreal` Python is optional in Editor and unavailable in packaged games and UE 4.18.
- Reflection reaches loaded objects and supported nonlatent UFunctions. Project tools own asynchronous operations and application-specific behavior; Editor-only APIs do not become Game APIs.
- Use an interactive Windows desktop for visible CEF and scene rendering. A CI service or NullRHI acceptance run does not certify the visible demo.
- If the engine/compiler is missing, configure the installed engine's supported toolchain and retry with a fresh output. Consult [build requirements and receipts](building.md).
- If identity or integrity checks reject reuse, use the matching verified package or prepare a fresh output. Do not copy individual DLLs between versions.

See [Python tools](python-tools.md) to register your own tools and [demo architecture](demo-architecture.md) to adapt the scene and dashboard.
