#if WITH_DEV_AUTOMATION_TESTS
#include "AuroraViewEditorModule.h"
#include "AuroraViewCompatibility.h"
#include "Dom/JsonObject.h"
#include "Framework/Application/SlateApplication.h"
#include "Framework/Docking/TabManager.h"
#include "GenericPlatform/GenericWindow.h"
#include "HAL/PlatformTime.h"
#include "LevelEditor.h"
#include "Misc/AutomationTest.h"
#include "Modules/ModuleManager.h"
#include "Widgets/Docking/SDockTab.h"
#include "Widgets/SWindow.h"

namespace
{
const FName DockA(TEXT("DockAcceptanceA")), DockB(TEXT("DockAcceptanceB"));
const FName DetailsId(TEXT("LevelEditorSelectionDetails"));
const FString DockHtml = TEXT("<h1>Actual docked CEF lifecycle test</h1><script>")
    TEXT("addEventListener('auroraviewready',()=>auroraview.call('test.echo',{ok:true}).then(v=>auroraview.call('test.docked',v.ok===true)));</script>");
FString NativeWindowHandle(const TSharedPtr<SWindow>& Window)
{
    const auto Native = Window.IsValid() ? Window->GetNativeWindow() : TSharedPtr<FGenericWindow>();
    return Native.IsValid() ? AuroraViewCompatibility::UInt64String(
        reinterpret_cast<UPTRINT>(Native->GetOSWindowHandle())) : TEXT("none");
}
void AddDockDiagnostics(FAutomationTestBase* Test, const TSharedPtr<FTabManager>& Manager)
{
    const auto Root = FGlobalTabmanager::Get()->GetRootWindow();
    Test->AddInfo(TEXT("Dock diagnostic rootNativeHandle=") + NativeWindowHandle(Root));
    const auto Record = [Test, Root](FName LookupId, const TSharedPtr<SDockTab>& Tab)
    {
        const auto CachedWindow = Tab.IsValid() ? Tab->GetParentWindow() : TSharedPtr<SWindow>();
        const auto ActualWindow = Tab.IsValid()
            ? FSlateApplication::Get().FindWidgetWindow(Tab.ToSharedRef()) : TSharedPtr<SWindow>();
        Test->AddInfo(FString::Printf(TEXT("Dock diagnostic lookup=%s exists=%d layoutId=%s ")
            TEXT("cachedAreaWindow=%s actualWidgetWindow=%s actualAtRoot=%d"),
            *LookupId.ToString(), Tab.IsValid(),
            Tab.IsValid() ? *Tab->GetLayoutIdentifier().ToString() : TEXT("none"),
            *NativeWindowHandle(CachedWindow), *NativeWindowHandle(ActualWindow),
            Root.IsValid() && ActualWindow == Root));
    };
    Record(FName(TEXT("LevelEditorOwner")), Manager.IsValid() ? Manager->GetOwnerTab() : TSharedPtr<SDockTab>());
    const FName LookupIds[] = { DetailsId, FName(TEXT("LevelEditorViewport")),
        FName(TEXT("LevelEditorViewport_Clone1")), FName(TEXT("LevelEditorViewport_Clone2")),
        FName(TEXT("LevelEditorViewport_Clone3")), FName(TEXT("LevelEditorViewport_Clone4")),
        FName(TEXT("AuroraView.View.DockAcceptanceA")), FName(TEXT("AuroraView.View.DockAcceptanceB")) };
    for (const FName LookupId : LookupIds)
        Record(LookupId, Manager.IsValid() ? Manager->FindExistingLiveTab(LookupId) : TSharedPtr<SDockTab>());
}
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
    int32 EchoesA = 0, EchoesB = 0;
    int32 ReceivedReportsA = 0, ReceivedReportsB = 0;
    int32 Phase = 0;
    uint64 FirstGeneration = 0;
    bool bObservedDetailsClosed = false;
    FString FirstLayoutId;
    FString LastWait = TEXT("initial CEF echo/report");
    FString LastDockErrorCode, LastDockErrorMessage;
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
            const auto A = Runtime.DescribeView(DockA), B = Runtime.DescribeView(DockB);
            const auto TargetManager = FModuleManager::GetModuleChecked<FLevelEditorModule>(TEXT("LevelEditor")).GetLevelEditorTabManager();
            const auto Root = FGlobalTabmanager::Get()->GetRootWindow();
            const auto Owner = TargetManager.IsValid() ? TargetManager->GetOwnerTab() : TSharedPtr<SDockTab>();
            const auto Details = TargetManager.IsValid() ? TargetManager->FindExistingLiveTab(DetailsId) : TSharedPtr<SDockTab>();
            Test->AddError(FString::Printf(TEXT("Timed out waiting for actual docked CEF round trips / reopen: ")
                TEXT("phase=%d wait=%s echoesA=%d echoesB=%d reportsA=%d reportsB=%d receivedReportsA=%d receivedReportsB=%d ")
                TEXT("detailsClosedObserved=%d manager=%d root=%d owner=%d ownerAtRoot=%d details=%d detailsAtRoot=%d ")
                TEXT("readyA=%d readyB=%d tabOpenA=%d tabOpenB=%d nativeRootAttachedA=%d nativeRootAttachedB=%d ")
                TEXT("lastDockErrorCode=%s lastDockErrorMessage=%s"),
                State->Phase, *State->LastWait, State->EchoesA, State->EchoesB,
                State->ReportsA, State->ReportsB, State->ReceivedReportsA, State->ReceivedReportsB,
                State->bObservedDetailsClosed, TargetManager.IsValid(), Root.IsValid(), Owner.IsValid(),
                Owner.IsValid() && Root.IsValid() && FSlateApplication::Get().FindWidgetWindow(Owner.ToSharedRef()) == Root,
                Details.IsValid(), Details.IsValid() && Root.IsValid() && FSlateApplication::Get().FindWidgetWindow(Details.ToSharedRef()) == Root,
                A->GetBoolField(TEXT("ready")), B->GetBoolField(TEXT("ready")),
                A->GetBoolField(TEXT("tab_open")), B->GetBoolField(TEXT("tab_open")),
                A->GetBoolField(TEXT("attached_to_root_window")), B->GetBoolField(TEXT("attached_to_root_window")),
                *State->LastDockErrorCode, *State->LastDockErrorMessage));
            AddDockDiagnostics(Test, TargetManager);
            Module.Remove(DockA); Module.Remove(DockB);
            return true;
        }
        if (State->Phase == 0)
        {
            if (State->ReportsA != 1 || State->ReportsB != 1) return false;
            Test->AddInfo(TEXT("DockedLifecycle phase 0: both actual CEF echo/report round trips completed"));
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
            const auto ActualNativeWindow = FSlateApplication::Get().FindWidgetWindow(NativeTab.ToSharedRef());
            Test->TestEqual(TEXT("Describe distinguishes root docking from a floating native tab"), NativeState->GetBoolField(TEXT("attached_to_root_window")),
                ActualNativeWindow.IsValid() && ActualNativeWindow == FGlobalTabmanager::Get()->GetRootWindow());
            Test->TestEqual(TEXT("CEF binding follows the actual tab host window"), NativeState->GetStringField(TEXT("browser_parent_window_native_handle")), NativeState->GetStringField(TEXT("window_native_handle")));
            Test->TestTrue(TEXT("Repeated control open reuses its actual native tab"), OpenControlDock(Runtime, DockA).bOk);
            Test->TestEqual(TEXT("Idempotent control open preserves the generation"), Module.GetGeneration(DockA), State->FirstGeneration);
            const auto Changed = OpenControlDock(Runtime, DockA, TEXT("<p>Cannot replace live content</p>"));
            Test->TestTrue(TEXT("Control open rejects changing live dock content"), !Changed.bOk && Changed.ErrorCode == TEXT("VIEW_OPEN_FAILED"));
            Test->TestEqual(TEXT("Rejected rebind preserves the current browser"), Module.GetGeneration(DockA), State->FirstGeneration);
            const auto TargetManager = FModuleManager::LoadModuleChecked<FLevelEditorModule>(TEXT("LevelEditor")).GetLevelEditorTabManager();
            const auto Details = TargetManager.IsValid()
                ? AuroraViewCompatibility::TryInvokeTab(TargetManager.ToSharedRef(), DetailsId) : TSharedPtr<SDockTab>();
            if (!Details.IsValid() || !Details->RequestCloseTab())
            {
                Test->AddError(TEXT("Could not close the native Details tab for the docking readiness regression"));
                Module.Remove(DockA); Module.Remove(DockB); return true;
            }
            State->Phase = 1;
            State->LastWait = TEXT("native Details tab removal");
            return false;
        }
        if (State->Phase == 1)
        {
            const auto TargetManager = FModuleManager::GetModuleChecked<FLevelEditorModule>(TEXT("LevelEditor")).GetLevelEditorTabManager();
            if (!TargetManager.IsValid()) return false;
            if (!State->bObservedDetailsClosed)
            {
                if (TargetManager->FindExistingLiveTab(DetailsId).IsValid()) return false;
                State->bObservedDetailsClosed = true;
                Test->AddInfo(TEXT("DockedLifecycle phase 1: native Details tab removal observed"));
                FString Error;
                Test->TestFalse(TEXT("Runtime refuses a closed native destination until the Editor tool opens it"),
                    Runtime.DockInTabManager(DockA, TargetManager.ToSharedRef(), DetailsId, Error));
                Test->TestEqual(TEXT("Rejected destination preserves the current browser generation"), Module.GetGeneration(DockA), State->FirstGeneration);
            }
            const auto DockReply = AttachControlDock(Runtime, DockA);
            State->LastWait = TEXT("registered Editor docking tool readiness");
            State->LastDockErrorCode = DockReply.ErrorCode;
            State->LastDockErrorMessage = DockReply.ErrorMessage;
            if (!DockReply.bOk && DockReply.ErrorCode == TEXT("EDITOR_UNAVAILABLE")) return false;
            if (!DockReply.bOk)
            {
                Test->AddError(TEXT("Native Editor root attachment failed: ") + DockReply.ErrorMessage);
                AddDockDiagnostics(Test, TargetManager);
                Module.Remove(DockA); Module.Remove(DockB); return true;
            }
            Test->TestTrue(TEXT("Registered Editor docking tool reopens the closed native Details destination"),
                TargetManager->FindExistingLiveTab(DetailsId).IsValid());
            const auto NativeTab = TargetManager->FindExistingLiveTab(FName(TEXT("AuroraView.View.DockAcceptanceA")));
            if (!NativeTab.IsValid())
            { Test->AddError(TEXT("Attached native tab is missing from the Level Editor manager")); Module.Remove(DockA); Module.Remove(DockB); return true; }
            const auto Attached = Runtime.DescribeView(DockA);
            Test->TestTrue(TEXT("Native dock move attaches the actual tab to the Editor root"), Attached->GetBoolField(TEXT("attached_to_root_window")));
            const auto ActualWindow = FSlateApplication::Get().FindWidgetWindow(NativeTab.ToSharedRef());
            Test->TestTrue(TEXT("Actual widget ancestry reaches the Editor root SWindow"), ActualWindow.IsValid()
                && ActualWindow == FGlobalTabmanager::Get()->GetRootWindow());
            Test->TestTrue(TEXT("The verified Editor root has a real native window handle"),
                ActualWindow.IsValid() && ActualWindow->GetNativeWindow().IsValid()
                    && ActualWindow->GetNativeWindow()->GetOSWindowHandle() != nullptr);
            Test->TestEqual(TEXT("Describe reports the independently resolved native widget window handle"),
                Attached->GetStringField(TEXT("window_native_handle")), NativeWindowHandle(ActualWindow));
            AddDockDiagnostics(Test, TargetManager);
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
            State->Phase = 2;
            State->LastWait = TEXT("native A tab removal after Close");
            Test->AddInfo(TEXT("DockedLifecycle phase 2: native root attachment verified; A closed"));
            return false;
        }
        if (State->Phase == 2)
        {
            if (FGlobalTabmanager::Get()->FindExistingLiveTab(FName(TEXT("AuroraView.View.DockAcceptanceA"))).IsValid())
                return false;
            const auto Reply = OpenControlDock(Runtime, DockA);
            if (!Reply.bOk) Test->AddError(Reply.ErrorMessage);
            State->Phase = 3;
            State->LastWait = TEXT("reopened CEF echo/report round trip");
            Test->AddInfo(TEXT("DockedLifecycle phase 3: same-ID native tab reopened"));
            return false;
        }
        if (State->ReportsA != 2) return false;
        Test->TestTrue(TEXT("Same-ID reopen uses a fresh generation"), Module.GetGeneration(DockA) > State->FirstGeneration);
        const uint64 ReopenedGeneration = Module.GetGeneration(DockA);
        const auto TargetManager = FModuleManager::GetModuleChecked<FLevelEditorModule>(TEXT("LevelEditor")).GetLevelEditorTabManager();
        const auto Destination = TargetManager.IsValid() ? TargetManager->FindExistingLiveTab(DetailsId) : TSharedPtr<SDockTab>();
        const auto DockReply = AttachControlDock(Runtime, DockA);
        State->LastWait = TEXT("reopened registered Editor docking tool readiness");
        State->LastDockErrorCode = DockReply.ErrorCode;
        State->LastDockErrorMessage = DockReply.ErrorMessage;
        if (!DockReply.bOk && DockReply.ErrorCode == TEXT("EDITOR_UNAVAILABLE")) return false;
        Test->TestTrue(TEXT("Same-ID reopened browser can attach to its native Editor stack again"), TargetManager.IsValid() && DockReply.bOk);
        const auto Reattached = Runtime.DescribeView(DockA);
        Test->TestTrue(TEXT("Reopened browser is actually attached to the Editor root"), Reattached->GetBoolField(TEXT("attached_to_root_window")));
        const auto ReopenedTab = TargetManager.IsValid()
            ? TargetManager->FindExistingLiveTab(FName(TEXT("AuroraView.View.DockAcceptanceA"))) : TSharedPtr<SDockTab>();
        const auto ReopenedWindow = ReopenedTab.IsValid()
            ? FSlateApplication::Get().FindWidgetWindow(ReopenedTab.ToSharedRef()) : TSharedPtr<SWindow>();
        Test->TestTrue(TEXT("Reopened tab widget ancestry independently reaches the Editor root"),
            ReopenedWindow.IsValid() && ReopenedWindow == FGlobalTabmanager::Get()->GetRootWindow());
        Test->TestEqual(TEXT("Reopened Describe agrees with the actual native widget window handle"),
            Reattached->GetStringField(TEXT("window_native_handle")), NativeWindowHandle(ReopenedWindow));
        Test->TestEqual(TEXT("Reattaching does not restart the reopened browser"), Module.GetGeneration(DockA), ReopenedGeneration);
        Test->TestTrue(TEXT("Slate assigns a new transient document instance on reopened attachment"), Reattached->GetStringField(TEXT("tab_layout_id")) != State->FirstLayoutId);
        Test->TestTrue(TEXT("Attachment preserves the existing native destination tab"), Destination.IsValid()
            && TargetManager->FindExistingLiveTab(DetailsId) == Destination);
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
        Module.BindCall(Id, TEXT("test.echo"), [State, Id](const TSharedPtr<FJsonValue>& Params)
        {
            if (Id == DockA) ++State->EchoesA; else ++State->EchoesB;
            return FAuroraViewReply::Success(Params);
        });
        Module.BindCall(Id, TEXT("test.docked"), [State, Id](const TSharedPtr<FJsonValue>& Params)
        {
            if (Id == DockA) ++State->ReceivedReportsA; else ++State->ReceivedReportsB;
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
