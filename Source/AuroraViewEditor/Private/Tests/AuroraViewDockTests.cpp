#if WITH_DEV_AUTOMATION_TESTS
#include "AuroraViewEditorModule.h"
#include "Dom/JsonObject.h"
#include "Framework/Docking/TabManager.h"
#include "HAL/PlatformTime.h"
#include "LevelEditor.h"
#include "Misc/AutomationTest.h"
#include "Modules/ModuleManager.h"
#include "Widgets/Docking/SDockTab.h"

namespace
{
const FName DockA(TEXT("DockAcceptanceA")), DockB(TEXT("DockAcceptanceB"));
const FString DockHtml = TEXT("<h1>Actual docked CEF lifecycle test</h1><script>")
    TEXT("addEventListener('auroraviewready',()=>auroraview.call('test.echo',{ok:true}).then(v=>auroraview.call('test.docked',v.ok===true)));</script>");
FAuroraViewReply OpenControlDock(FAuroraViewRuntimeModule& Module, FName Id, const FString& Html = DockHtml)
{
    auto Args = MakeShared<FJsonObject>();
    Args->SetStringField(TEXT("id"), Id.ToString());
    Args->SetStringField(TEXT("html"), Html);
    Args->SetStringField(TEXT("title"), Id.ToString());
    Args->SetStringField(TEXT("presentation"), TEXT("docked"));
    FAuroraViewReply Reply;
    bool bCompleted = false;
    Module.CallTool(TEXT("auroraview.view.open"), MakeShared<FJsonValueObject>(Args),
        [&Reply, &bCompleted](FAuroraViewReply Value) { Reply = MoveTemp(Value); bCompleted = true; });
    check(bCompleted);
    return Reply;
}
FAuroraViewReply AttachControlDock(FAuroraViewRuntimeModule& Module, FName Id)
{
    auto Args = MakeShared<FJsonObject>();
    Args->SetStringField(TEXT("id"), Id.ToString());
    FAuroraViewReply Reply;
    bool bCompleted = false;
    Module.CallTool(TEXT("editor.view.dock"), MakeShared<FJsonValueObject>(Args),
        [&Reply, &bCompleted](FAuroraViewReply Value) { Reply = MoveTemp(Value); bCompleted = true; });
    check(bCompleted);
    return Reply;
}
struct FDockState
{
    int32 ReportsA = 0, ReportsB = 0;
    int32 Phase = 0;
    uint64 FirstGeneration = 0;
    FString FirstLayoutId;
    double Deadline = 0;
};
class FWaitForDocked final : public IAutomationLatentCommand
{
public:
    FWaitForDocked(FAutomationTestBase* InTest, TSharedRef<FDockState> InState) : Test(InTest), State(InState) {}
    bool Update() override
    {
        auto& Module = FModuleManager::GetModuleChecked<FAuroraViewEditorModule>(TEXT("AuroraViewEditor"));
        auto& Runtime = FModuleManager::GetModuleChecked<FAuroraViewRuntimeModule>(TEXT("AuroraViewRuntime"));
        if (FPlatformTime::Seconds() > State->Deadline)
        {
            Test->AddError(TEXT("Timed out waiting for actual docked CEF round trips / reopen"));
            Module.Remove(DockA); Module.Remove(DockB);
            return true;
        }
        if (State->Phase == 0)
        {
            if (State->ReportsA != 1 || State->ReportsB != 1) return false;
            Test->TestTrue(TEXT("Both docked browsers are ready"), Module.IsReady(DockA) && Module.IsReady(DockB));
            Test->TestTrue(TEXT("A is an actual registered live Slate dock"),
                FGlobalTabmanager::Get()->FindExistingLiveTab(FName(TEXT("AuroraView.View.DockAcceptanceA"))).IsValid());
            Test->TestTrue(TEXT("B is an independent live Slate dock"),
                FGlobalTabmanager::Get()->FindExistingLiveTab(FName(TEXT("AuroraView.View.DockAcceptanceB"))).IsValid());
            Test->TestFalse(TEXT("Floating-only Hide never pretends to hide a dock"), Module.Hide(DockA));
            State->FirstGeneration = Module.GetGeneration(DockA);
            const auto NativeTab = FGlobalTabmanager::Get()->FindExistingLiveTab(FName(TEXT("AuroraView.View.DockAcceptanceA")));
            if (!NativeTab.IsValid())
            { Test->AddError(TEXT("Expected a live owned native tab")); Module.Remove(DockA); Module.Remove(DockB); return true; }
            const auto NativeState = Runtime.DescribeView(DockA);
            Test->TestEqual(TEXT("Describe identifies the actual native tab presentation"), NativeState->GetStringField(TEXT("presentation")), FString(TEXT("docked")));
            Test->TestTrue(TEXT("Describe reports the live tab and registration"), NativeState->GetBoolField(TEXT("tab_open")) && NativeState->GetBoolField(TEXT("dock_registered")));
            Test->TestEqual(TEXT("Describe distinguishes root docking from a floating native tab"), NativeState->GetBoolField(TEXT("attached_to_root_window")),
                NativeTab.IsValid() && NativeTab->GetParentWindow().IsValid() && NativeTab->GetParentWindow() == FGlobalTabmanager::Get()->GetRootWindow());
            Test->TestEqual(TEXT("CEF binding follows the actual tab host window"), NativeState->GetStringField(TEXT("browser_parent_window_native_handle")), NativeState->GetStringField(TEXT("window_native_handle")));
            Test->TestTrue(TEXT("Repeated control open reuses its actual native tab"), OpenControlDock(Runtime, DockA).bOk);
            Test->TestEqual(TEXT("Idempotent control open preserves the generation"), Module.GetGeneration(DockA), State->FirstGeneration);
            const auto Changed = OpenControlDock(Runtime, DockA, TEXT("<p>Cannot replace live content</p>"));
            Test->TestTrue(TEXT("Control open rejects changing live dock content"), !Changed.bOk && Changed.ErrorCode == TEXT("VIEW_OPEN_FAILED"));
            Test->TestEqual(TEXT("Rejected rebind preserves the current browser"), Module.GetGeneration(DockA), State->FirstGeneration);
            const auto TargetManager = FModuleManager::LoadModuleChecked<FLevelEditorModule>(TEXT("LevelEditor")).GetLevelEditorTabManager();
            const auto DockReply = AttachControlDock(Runtime, DockA);
            if (!TargetManager.IsValid() || !DockReply.bOk)
            {
                Test->AddError(TEXT("Native Editor root attachment failed: ") + DockReply.ErrorMessage);
                Module.Remove(DockA); Module.Remove(DockB); return true;
            }
            const auto Attached = Runtime.DescribeView(DockA);
            Test->TestTrue(TEXT("Native dock move attaches the actual tab to the Editor root"), Attached->GetBoolField(TEXT("attached_to_root_window")));
            Test->TestTrue(TEXT("Native dock move retains browser readiness"), Attached->GetBoolField(TEXT("ready")));
            Test->TestEqual(TEXT("Native dock move preserves browser generation"), Module.GetGeneration(DockA), State->FirstGeneration);
            Test->TestEqual(TEXT("Native docking preserves the registered private tab type"), NativeTab->GetLayoutIdentifier().TabType, FName(TEXT("AuroraView.View.DockAcceptanceA")));
            State->FirstLayoutId = Attached->GetStringField(TEXT("tab_layout_id"));
            Test->TestEqual(TEXT("Native docking rebinds the CEF parent to the root window"), Attached->GetStringField(TEXT("browser_parent_window_native_handle")), Attached->GetStringField(TEXT("root_window_native_handle")));
            Test->TestTrue(TEXT("Idempotent open preserves actual root attachment"), OpenControlDock(Runtime, DockA).bOk && Runtime.DescribeView(DockA)->GetBoolField(TEXT("attached_to_root_window")));
            Module.Close(DockA);
            Test->TestFalse(TEXT("Close invalidates A readiness synchronously"), Module.IsReady(DockA));
            Test->TestTrue(TEXT("Close A preserves B"), Module.IsReady(DockB));
            const auto Closed = Runtime.DescribeView(DockA);
            Test->TestTrue(TEXT("Closed native state retains registration but no live presentation"), Closed->GetBoolField(TEXT("dock_registered")) && !Closed->GetBoolField(TEXT("tab_open")) && !Closed->GetBoolField(TEXT("open")) && Closed->GetNumberField(TEXT("generation")) == 0);
            State->Phase = 1;
            return false;
        }
        if (State->Phase == 1)
        {
            if (FGlobalTabmanager::Get()->FindExistingLiveTab(FName(TEXT("AuroraView.View.DockAcceptanceA"))).IsValid())
                return false;
            const auto Reply = OpenControlDock(Runtime, DockA);
            if (!Reply.bOk) Test->AddError(Reply.ErrorMessage);
            State->Phase = 2;
            return false;
        }
        if (State->ReportsA != 2) return false;
        Test->TestTrue(TEXT("Same-ID reopen uses a fresh generation"), Module.GetGeneration(DockA) > State->FirstGeneration);
        const uint64 ReopenedGeneration = Module.GetGeneration(DockA);
        const auto TargetManager = FModuleManager::GetModuleChecked<FLevelEditorModule>(TEXT("LevelEditor")).GetLevelEditorTabManager();
        const auto Destination = TargetManager.IsValid() ? TargetManager->FindExistingLiveTab(FName(TEXT("LevelEditorSelectionDetails"))) : TSharedPtr<SDockTab>();
        Test->TestTrue(TEXT("Same-ID reopened browser can attach to its native Editor stack again"),
            TargetManager.IsValid() && AttachControlDock(Runtime, DockA).bOk);
        const auto Reattached = Runtime.DescribeView(DockA);
        Test->TestTrue(TEXT("Reopened browser is actually attached to the Editor root"), Reattached->GetBoolField(TEXT("attached_to_root_window")));
        Test->TestEqual(TEXT("Reattaching does not restart the reopened browser"), Module.GetGeneration(DockA), ReopenedGeneration);
        Test->TestTrue(TEXT("Slate assigns a new transient document instance on reopened attachment"), Reattached->GetStringField(TEXT("tab_layout_id")) != State->FirstLayoutId);
        Test->TestTrue(TEXT("Attachment preserves the existing native destination tab"), Destination.IsValid()
            && TargetManager->FindExistingLiveTab(FName(TEXT("LevelEditorSelectionDetails"))) == Destination);
        Test->TestEqual(TEXT("B did not reload during A close/reopen"), State->ReportsB, 1);
        Test->TestTrue(TEXT("Remove A succeeds"), Module.Remove(DockA));
        Test->TestTrue(TEXT("Remove B succeeds"), Module.Remove(DockB));
        return true;
    }
private:
    FAutomationTestBase* Test;
    TSharedRef<FDockState> State;
};
}
IMPLEMENT_SIMPLE_AUTOMATION_TEST(FAuroraViewDockAcceptance, "AuroraView.Editor.DockedLifecycle",
    EAutomationTestFlags::EditorContext | EAutomationTestFlags::EngineFilter)
bool FAuroraViewDockAcceptance::RunTest(const FString&)
{
    auto& Module = FModuleManager::LoadModuleChecked<FAuroraViewEditorModule>(TEXT("AuroraViewEditor"));
    auto& Runtime = FModuleManager::GetModuleChecked<FAuroraViewRuntimeModule>(TEXT("AuroraViewRuntime"));
    const auto State = MakeShared<FDockState>();
    State->Deadline = FPlatformTime::Seconds() + 35.0;
    for (const FName Id : { DockA, DockB })
    {
        Module.Remove(Id);
        Module.BindCall(Id, TEXT("test.echo"), [](const TSharedPtr<FJsonValue>& Params)
            { return FAuroraViewReply::Success(Params); });
        Module.BindCall(Id, TEXT("test.docked"), [State, Id](const TSharedPtr<FJsonValue>& Params)
        {
            if (Params.IsValid() && Params->Type == EJson::Boolean && Params->AsBool())
            { if (Id == DockA) ++State->ReportsA; else ++State->ReportsB; }
            return FAuroraViewReply::Success(MakeShared<FJsonValueBoolean>(IsInGameThread()));
        });
        const auto Reply = OpenControlDock(Runtime, Id);
        if (!Reply.bOk)
        {
            AddError(Reply.ErrorMessage); Module.Remove(DockA); Module.Remove(DockB); return false;
        }
    }
    ADD_LATENT_AUTOMATION_COMMAND(FWaitForDocked(this, State));
    return true;
}
#endif
