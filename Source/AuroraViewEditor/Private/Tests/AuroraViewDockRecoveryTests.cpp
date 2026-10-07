#if WITH_DEV_AUTOMATION_TESTS
#include "AuroraViewEditorModule.h"
#include "Framework/Docking/TabManager.h"
#include "HAL/PlatformTime.h"
#include "Misc/AutomationTest.h"
#include "Modules/ModuleManager.h"
#include "Widgets/Docking/SDockTab.h"

namespace
{
const FName RetryId(TEXT("DockFailedSpawnRetry")), RemoveId(TEXT("DockFailedSpawnRemove")), ClosedId(TEXT("DockFailedSpawnManuallyClosed"));
struct FRecoveryState { bool bBrowserReplied = false, bClosedReopened = false; double Deadline = 0; TSharedPtr<SDockTab> RetainedClosedTab; };
class FWaitForRecovery final : public IAutomationLatentCommand
{
public:
    FWaitForRecovery(FAutomationTestBase* InTest, TSharedRef<FRecoveryState> InState) : Test(InTest), State(InState) {}
    bool Update() override
    {
        auto& Module = FModuleManager::GetModuleChecked<FAuroraViewEditorModule>(TEXT("AuroraViewEditor"));
        if (!State->bClosedReopened)
        {
            const FName TabId(TEXT("AuroraView.View.DockFailedSpawnManuallyClosed"));
            if (FGlobalTabmanager::Get()->FindExistingLiveTab(TabId).IsValid()
                && FPlatformTime::Seconds() < State->Deadline) return false;
            FString Error;
            Test->TestTrue(TEXT("Manually closed failed tab can reopen"), Module.OpenDocked(ClosedId, Error));
            const auto NewTab = FGlobalTabmanager::Get()->FindExistingLiveTab(TabId);
            Test->TestTrue(TEXT("Reopen owns a new live tab while closed error tab is retained"), NewTab.IsValid() && NewTab != State->RetainedClosedTab);
            State->bClosedReopened = true;
        }
        const bool bRemoved = !FGlobalTabmanager::Get()->FindExistingLiveTab(
            FName(TEXT("AuroraView.View.DockFailedSpawnRemove"))).IsValid();
        if ((!State->bBrowserReplied || !bRemoved || !Module.IsReady(ClosedId)) && FPlatformTime::Seconds() < State->Deadline) return false;
        Test->TestTrue(TEXT("Retried error tab receives an actual Core echo reply"), State->bBrowserReplied);
        Test->TestTrue(TEXT("Remove retires the failed-spawn error tab"), bRemoved);
        Test->TestTrue(TEXT("Reopened replacement reaches actual native readiness"), Module.IsReady(ClosedId));
        Module.Remove(RetryId); Module.Remove(RemoveId); Module.Remove(ClosedId);
        State->RetainedClosedTab.Reset();
        return true;
    }
private:
    FAutomationTestBase* Test; TSharedRef<FRecoveryState> State;
};
}
IMPLEMENT_SIMPLE_AUTOMATION_TEST(FAuroraViewFailedDockRecovery, "AuroraView.Editor.FailedDockRecovery",
    EAutomationTestFlags::EditorContext | EAutomationTestFlags::EngineFilter)
bool FAuroraViewFailedDockRecovery::RunTest(const FString&)
{
    auto& Module = FModuleManager::LoadModuleChecked<FAuroraViewEditorModule>(TEXT("AuroraViewEditor"));
    Module.Remove(RetryId); Module.Remove(RemoveId); Module.Remove(ClosedId);
    const auto State = MakeShared<FRecoveryState>(); State->Deadline = FPlatformTime::Seconds() + 20;
    const FString Html = TEXT("<h1>Native failed-spawn recovery</h1><script>")
        TEXT("addEventListener('auroraviewready',()=>auroraview.call('test.echo',{v:73})")
        TEXT(".then(v=>auroraview.call('test.report',v.v===73)));</script>");
    Module.BindCall(RetryId, TEXT("test.echo"), [](const TSharedPtr<FJsonValue>& Params)
        { return FAuroraViewReply::Success(Params); });
    Module.BindCall(RetryId, TEXT("test.report"), [State](const TSharedPtr<FJsonValue>& Params)
    {
        State->bBrowserReplied = Params.IsValid() && Params->Type == EJson::Boolean && Params->AsBool();
        return FAuroraViewReply::Success(MakeShared<FJsonValueBoolean>(true));
    });
    FString Error;
    for (const FName Id : { RetryId, RemoveId, ClosedId })
    {
        if (!Module.RegisterDocked(Id, Html, FText::FromName(Id), Error)) { AddError(Error); return false; }
        Module.FailNextOpenForTesting(Id);
        TestFalse(TEXT("Intentional native pre-browser failure reports false"), Module.OpenDocked(Id, Error));
        TestTrue(TEXT("Actual failure reason reaches the caller"), Error.Contains(TEXT("Intentional native failed-spawn")));
        TestFalse(TEXT("Failed spawn is not a ready browser"), Module.IsReady(Id));
        TestTrue(TEXT("Failure still produces an honestly labeled native error tab"),
            FGlobalTabmanager::Get()->FindExistingLiveTab(FName(*(TEXT("AuroraView.View.") + Id.ToString()))).IsValid());
    }
    if (!Module.OpenDocked(RetryId, Error))
    { AddError(Error); Module.Remove(RetryId); Module.Remove(RemoveId); return false; }
    TestTrue(TEXT("Remove accepts a managed failed-spawn tab"), Module.Remove(RemoveId));
    State->RetainedClosedTab = FGlobalTabmanager::Get()->FindExistingLiveTab(FName(TEXT("AuroraView.View.DockFailedSpawnManuallyClosed")));
    if (!State->RetainedClosedTab.IsValid()) { AddError(TEXT("Expected owned failed-spawn tab")); return false; }
    State->RetainedClosedTab->RequestCloseTab();
    ADD_LATENT_AUTOMATION_COMMAND(FWaitForRecovery(this, State));
    return true;
}
#endif
