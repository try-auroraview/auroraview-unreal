# Compose a docked browser with native Slate

`FAuroraViewRuntimeModule::RegisterDocked` already accepts a public `FAuroraViewDockContent` factory. It receives the module-owned `SDockTab` and browser `SWidget` on GameThread. Return a Slate widget tree containing that browser and your native controls; Runtime installs the tree in the same tab. No second browser, IPC protocol, Rust ABI or Python extension is needed.

The existing consumer is `FAuroraViewNativeShowcase::MakeWorkspace` in `Source/AuroraViewEditor/Private/AuroraViewNativeShowcase.cpp`. It combines the browser with native drag controls, a scene outliner and an asset picker. The example below demonstrates the smaller public contract. It is documentation code, not a separately compiled example target; native builds and real rendered acceptance must be recorded for the exact source being delivered.

## Public consumer example

Add `AuroraViewRuntime`, `Slate` and `SlateCore` to your Editor module dependencies. Create and retain this owner after Slate is initialized. Call `Start` on GameThread with trusted HTML, and call `Stop` during your module's shutdown before Runtime unloads.

```cpp
#include "AuroraViewRuntimeModule.h"
#include "Dom/JsonObject.h"
#include "Input/Reply.h"
#include "Modules/ModuleManager.h"
#include "Widgets/Input/SButton.h"
#include "Widgets/Layout/SSplitter.h"
#include "Widgets/Text/STextBlock.h"

class FProjectPanel : public TSharedFromThis<FProjectPanel>
{
public:
    bool Start(const FString& TrustedHtml, FString& Error)
    {
        check(IsInGameThread());
        auto& Runtime = FModuleManager::LoadModuleChecked<FAuroraViewRuntimeModule>(
            TEXT("AuroraViewRuntime"));
        if (!bRegistered)
        {
            const TWeakPtr<FProjectPanel> WeakOwner = AsShared();
            bRegistered = Runtime.RegisterDocked(Id(), TrustedHtml,
                FText::FromString(TEXT("Project Inspector")), Error,
                [WeakOwner](const TSharedRef<SDockTab>&,
                            const TSharedRef<SWidget>& Browser) -> TSharedRef<SWidget>
                {
                    const auto Owner = WeakOwner.Pin();
                    return Owner.IsValid() ? Owner->Compose(Browser) : Browser;
                });
            if (!bRegistered) return false;
        }
        return Runtime.OpenDocked(Id(), Error);
    }

    void Stop()
    {
        check(IsInGameThread());
        if (bRegistered)
        {
            if (auto* Runtime = FModuleManager::GetModulePtr<FAuroraViewRuntimeModule>(
                TEXT("AuroraViewRuntime")))
            {
                Runtime->Remove(Id());
            }
            bRegistered = false;
        }
    }

private:
    static FName Id() { return FName(TEXT("ProjectInspector")); }

    TSharedRef<SWidget> Compose(const TSharedRef<SWidget>& Browser)
    {
        auto& Runtime = FModuleManager::GetModuleChecked<FAuroraViewRuntimeModule>(
            TEXT("AuroraViewRuntime"));
        const uint64 Generation = Runtime.GetGeneration(Id());
        const TWeakPtr<FProjectPanel> WeakOwner = AsShared();
        return SNew(SSplitter)
            + SSplitter::Slot().Value(0.7f)[Browser]
            + SSplitter::Slot().Value(0.3f)
            [
                SNew(SButton)
                .OnClicked_Lambda([WeakOwner, Generation]()
                {
                    const auto Owner = WeakOwner.Pin();
                    auto* Current = FModuleManager::GetModulePtr<FAuroraViewRuntimeModule>(
                        TEXT("AuroraViewRuntime"));
                    if (Owner.IsValid() && Current && Current->IsReady(Id()) &&
                        Generation != 0 && Current->GetGeneration(Id()) == Generation)
                    {
                        auto Detail = MakeShared<FJsonObject>();
                        Detail->SetNumberField(TEXT("count"), ++Owner->NativeClicks);
                        Current->EmitEvent(Id(), TEXT("project:native.click"), Detail);
                    }
                    return FReply::Handled();
                })
                [SNew(STextBlock).Text(FText::FromString(TEXT("Native Slate event")))]
            ];
    }

    bool bRegistered = false;
    uint32 NativeClicks = 0;
};

// Retain this shared pointer in the consumer module, not a temporary variable:
// Panel = MakeShared<FProjectPanel>();
// if (!Panel->Start(TrustedHtml, Error)) { /* report Error; do not claim ready */ }
// ... consumer ShutdownModule: Panel->Stop(); Panel.Reset();
```

Browser content uses the pinned AuroraView Core JavaScript API to subscribe to `project:native.click` or call project tools. `OpenDocked` success means the native view was opened; wait for `IsReady` or the normal Core readiness contract before claiming communication readiness. External Python tools continue to use the existing authenticated standard-library client and declared methods. Python does not construct or serialize `SWidget` trees.

Runtime owns the browser, RPC endpoint, tab-close callback and presentation generation. Mount the supplied browser exactly once; do not reparent it into another window or replace the tab's `OnTabClosed`. Use weak owners and the captured live generation for native callbacks. Keep referenced UObjects weak and validate their world/context before acting. `Close` retains registration, handlers and the factory for reopening; `Remove` revokes the consumer's registration. Reopening creates a fresh browser generation, so callbacks from an old native widget must refuse work.

## Workspace and presentation boundaries

`DockInTabManager` can attach this live tab beside an already-open Editor root tab. It preserves the browser generation and verifies actual widget-window attachment. It does not restore an arbitrary user-dragged position or move a tab already attached to the root into another stack. Observe `DescribeView` and actual native window/parent state when verifying docking.

The private `SAuroraViewWorkspace` is a showcase implementation with fixed Inspector/Outliner/Assets IDs, layout name and saved file. A reusable multi-panel API should parameterize a caller-scoped workspace ID, panel IDs/content, default layout and an owned saved path before exporting it. Each outer tab must retain its own tab manager; callers must not overwrite the global Editor layout. Simple browser/native composition needs none of that additional API.

The admitted Win64 versions are UE 4.18, 4.26, 5.5, 5.7 and 5.8. Existing compatibility helpers adapt tab invocation and native browser lifecycle; sidebar controls remain UE5-only. Avoid adding UE5-only ToolMenus or Editor dependencies to Runtime. The floating Game path currently hosts the browser alone. This factory does not establish a viewport/UMG embed or a caller-owned floating-window API.

Source and native automation cover ownership, factory reentrancy, generation changes and actual root-window readback. Interactive acceptance still needs the delivered build: browser call/result/event readback beside native controls, dock/undock/redock, child-panel close/reopen, teardown and any claimed layout persistence. For multi-panel reuse, test two independently named workspaces and verify that their panels, saved files and callbacks remain isolated.
