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

Primary references for public API context:

- [TabManager](https://dev.epicgames.com/documentation/unreal-engine/API/Runtime/Slate/FTabManager)
- [Sidebar semantics](https://dev.epicgames.com/documentation/unreal-engine/API/Runtime/Slate/Framework/Docking/FTabManager/ToggleSidebarOpenTabs?application_version=5.5)

Online documentation may show a newer engine version. Exact installed UE 5.7
headers and graphical behavior remain verification gates. Cross-window CEF
activation and IME parent-cache rebinding are unverified.
