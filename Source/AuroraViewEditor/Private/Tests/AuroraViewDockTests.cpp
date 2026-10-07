#if WITH_DEV_AUTOMATION_TESTS
#include "AuroraViewEditorModule.h"
#include "Dom/JsonObject.h"
#include "Framework/Docking/TabManager.h"
#include "HAL/PlatformTime.h"
#include "Misc/AutomationTest.h"
#include "Modules/ModuleManager.h"
#include "Widgets/Docking/SDockTab.h"

namespace
{
const FName DockA(TEXT("DockAcceptanceA")), DockB(TEXT("DockAcceptanceB"));
struct FDockState
{
    int32 ReportsA = 0, ReportsB = 0;
    int32 Phase = 0;
    uint64 FirstGeneration = 0;
    double Deadline = 0;
};
class FWaitForDocked final : public IAutomationLatentCommand
{
public:
    FWaitForDocked(FAutomationTestBase* InTest, TSharedRef<FDockState> InState) : Test(InTest), State(InState) {}
    bool Update() override
    {
        auto& Module = FModuleManager::GetModuleChecked<FAuroraViewEditorModule>(TEXT("AuroraViewEditor"));
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
            Module.Close(DockA);
            Test->TestFalse(TEXT("Close invalidates A readiness synchronously"), Module.IsReady(DockA));
            Test->TestTrue(TEXT("Close A preserves B"), Module.IsReady(DockB));
            State->Phase = 1;
            return false;
        }
        if (State->Phase == 1)
        {
            if (FGlobalTabmanager::Get()->FindExistingLiveTab(FName(TEXT("AuroraView.View.DockAcceptanceA"))).IsValid())
                return false;
            FString Error;
            if (!Module.OpenDocked(DockA, Error)) Test->AddError(Error);
            State->Phase = 2;
            return false;
        }
        if (State->ReportsA != 2) return false;
        Test->TestTrue(TEXT("Same-ID reopen uses a fresh generation"), Module.GetGeneration(DockA) > State->FirstGeneration);
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
        FString Error;
        const FString Html = TEXT("<h1>Actual docked CEF lifecycle test</h1><script>")
            TEXT("addEventListener('auroraviewready',()=>auroraview.call('test.echo',{ok:true}).then(v=>auroraview.call('test.docked',v.ok===true)));</script>");
        if (!Module.RegisterDocked(Id, Html, FText::FromName(Id), Error) || !Module.OpenDocked(Id, Error))
        {
            AddError(Error); Module.Remove(DockA); Module.Remove(DockB); return false;
        }
    }
    ADD_LATENT_AUTOMATION_COMMAND(FWaitForDocked(this, State));
    return true;
}
#endif
