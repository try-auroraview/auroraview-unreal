# Unreal version policy

The explicit Win64 source/build matrix matches dcc-mcp-unreal: **4.18, 4.26, 5.5, 5.7 and 5.8**. Runtime and Editor modules are separate. Other versions/platforms remain outside this matrix until their own compilation and acceptance are added.

| Candidate | Local installed patch | Editor target | Embedded Editor Python | Browser bridge |
| --- | --- | --- | --- | --- |
| 4.18 | 4.18.3 | UE4Editor | Unavailable; external Python tools | Chrome 59 legacy bundle |
| 4.26 | 4.26.2 | UE4Editor | Optional PythonScriptPlugin | Chrome 59 legacy bundle |
| 5.5 | 5.5.4 | UnrealEditor | Optional PythonScriptPlugin | Original pinned Core |
| 5.7 | 5.7.4 | UnrealEditor | Optional PythonScriptPlugin | Original pinned Core |
| 5.8 | 5.8.1 | UnrealEditor | Optional PythonScriptPlugin | Original pinned Core |

These are current inventory facts, not acceptance results for a future commit. Inspect each build receipt and Automation/Game report for the exact source/engine tested. Never reuse DLLs across minors or BuildIds, even when C++ signatures or CEF versions match.

The compatibility adapter covers ticker/pre-exit, tab invocation/ownership, browser close/parent window, AssetData, Outliner and old UProperty versus FProperty reflection. UE4 has no sidebar extension. Native map preparation avoids relying on Editor Python. UE4's generated legacy bridge preserves pinned source hashes and records reproducible tool/input/output provenance.

Each supported version requires source contracts, actual UHT/UBT Editor and Development/Shipping Game products, real graphical Editor acceptance, and actual cooked Game control/Python/event acceptance. Rendered packaged Game CEF and manual native interactions are separate acceptance gates. A successful BuildPlugin run does not establish them.

See [commands and CI](building.md), [architecture](architecture.md) and historical [acceptance evidence](acceptance.md). The exact installed engine's headers and actual build are authoritative for native APIs; online documentation alone is not compiler evidence.
