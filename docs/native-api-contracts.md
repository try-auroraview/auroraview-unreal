# Native API contract audit for this source candidate

Declarations were checked against the installed UE 5.7 Win64 engine's public
headers during source preparation. This is header evidence, not compilation,
CEF execution, native interaction or release validation. Full UHT/UBT and real
Editor tests are required against the exact follow-up source tree.

- The dock owns its SWebBrowser as actual Slate content. Win64 CEF does not
  implement useful SetParentDockTab behavior; that method is not used
- Persistent tab identities use stable tab types, without InstanceId
- A private FTabManager owns its own SetOnPersistLayout callback. PersistLayout
  returns a layout; FLayoutSaveRestore writes it to the separate project file
- RestoreFrom returns a nullable widget; the code checks before ToSharedRef
- The verified 5.7 sidebar method is ToggleSidebarOpenTabs(), with no arguments.
  It moves eligible tabs from this manager's dock areas, or restores remembered
  live tabs. At least one tab stays in the dock area; visual-major and
  non-persistable tabs are ineligible. Restored layouts can populate the remembered
  list, so there is no local toggle-state boolean. Cross-manager moves of a
  remembered tab can affect its new stack; activation can update the global menu.
  The batch API does not independently honor tab locking. It is not a guessed
  per-tab ToggleSidebar API or an unconditional all-tabs/workspace-only action
- CreateActorBrowser(options, explicit weak world) supplies native actor-browsing
  mode and default columns. A raw default CreateSceneOutliner mode is not used
- AssetPicker uses Single selection, Tile view and native FOnAssetSelected;
  an invalid asset represents no selection. This is not a full multi-select API
- GetSelectedAssets takes a fresh empty array and reads the primary browser.
  With no primary browser, the engine does not clear old array contents
- IsLockLocation() const is the Editor actor transform lock, including the
  LevelInstance restriction; it is not a general selection/edit permission
- Content Browser SyncBrowserToAssets is deferred and returns no result; the
  source returns submitted, then waits for separately observed native selection
- FActorDragDropGraphEdOp::New accepts weak actor arrays and belongs to UnrealEd,
  despite its name. Its public inherited Actors field is used, not GetActors
- FAssetDragDropOp::New is given resolved asset metadata. Path-only native drag
  operations are not automatically resolved or treated as populated GetAssets

Primary references for public API context:

- [TabManager](https://dev.epicgames.com/documentation/unreal-engine/API/Runtime/Slate/FTabManager)
- [Sidebar semantics](https://dev.epicgames.com/documentation/unreal-engine/API/Runtime/Slate/Framework/Docking/FTabManager/ToggleSidebarOpenTabs?application_version=5.5)
- [Actor native drag operation and header](https://dev.epicgames.com/documentation/unreal-engine/API/Editor/UnrealEd/FActorDragDropGraphEdOp)
- [Actor-browser module](https://dev.epicgames.com/documentation/unreal-engine/API/Editor/SceneOutliner/FSceneOutlinerModule)
- [Native asset picker configuration](https://dev.epicgames.com/documentation/unreal-engine/API/Editor/ContentBrowser/FAssetPickerConfig)

Online documentation may default to a later engine version; its overloads are
not treated as proof of an installed engine's declarations. In particular, later
sidebar APIs may take exception arguments that the audited 5.7 API does not.

The installed SWebBrowserView/CEF/IME source audit also found stale-parent risks
across two live native windows: parent-window updates do not prove complete
activation-delegate/IME-cache rebinding. This is a source risk, not an observed
runtime bug. Cross-window focus/IME remains a dedicated blocked acceptance case;
no guessed private-engine patch or SetParentWindow-only fix is shipped.

## Indexed selection declarations checked during publication preparation

The installed UE 5.7.4 public headers were rechecked independently:

- `USelection::Num() const` returns `int32`
- `USelection::GetSelectedObject(const int32) const` returns `UObject*`; indexed
  access can expose null slots and does not compact them
- `AActor::IsActorBeingDestroyed() const` reads the actor destruction flag;
  it does not replace `IsValid` or weak-object validation

The indexed loop is not an atomic selection snapshot. The reviewed candidate
therefore revalidates the complete capture across callback boundaries and again
immediately before starting the native drag. This narrows the header-signature
gate only; UHT/UBT and native execution for the feature tree remain `not_run`.
