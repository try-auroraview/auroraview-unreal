#if WITH_DEV_AUTOMATION_TESTS
#include "AuroraViewEditorModule.h"
#include "Framework/Docking/TabManager.h"
#include "HAL/PlatformTime.h"
#include "Misc/AutomationTest.h"
#include "Modules/ModuleManager.h"
#include "Widgets/Docking/SDockTab.h"
#include "Widgets/SWidget.h"

namespace
{
const FName GrowId(TEXT("DockFactoryGrowth")), CloseId(TEXT("DockFactoryClose"));
const FName ReplaceId(TEXT("DockFactoryReplace")), ReopenId(TEXT("DockFactoryReopen"));
const FName IndependentId(TEXT("DockFactoryIndependent"));
const FString Html(TEXT("<h1>Native callback-boundary regression</h1>"));
struct FReentrantState { double Deadline = 0; uint64 IndependentGeneration = 0; };
class FWaitForReentrantDock final : public IAutomationLatentCommand
{
public:
    FWaitForReentrantDock(FAutomationTestBase* InTest, TSharedRef<FReentrantState> InState) : Test(InTest), State(InState) {}
    bool Update() override
    {
        auto& Module = FModuleManager::GetModuleChecked<FAuroraViewEditorModule>(TEXT("AuroraViewEditor"));
        bool bRetiredTabsGone = true;
        for (const FName Id : { CloseId, ReplaceId, ReopenId })
            bRetiredTabsGone &= !FGlobalTabmanager::Get()->FindExistingLiveTab(FName(*(TEXT("AuroraView.View.") + Id.ToString()))).IsValid();
        const bool bReady = Module.IsReady(GrowId) && Module.IsReady(IndependentId)
            && Module.IsReady(ReplaceId) && Module.IsReady(ReopenId);
        if ((!bReady || !bRetiredTabsGone) && FPlatformTime::Seconds() < State->Deadline) return false;
        Test->TestTrue(TEXT("Current presentations reach actual native CEF readiness"), bReady);
        Test->TestTrue(TEXT("Interrupted factories leave no orphan live tabs"), bRetiredTabsGone);
        Test->TestFalse(TEXT("Self-closed factory has no ready browser"), Module.IsReady(CloseId));
        Test->TestEqual(TEXT("Independent browser generation is unchanged"), Module.GetGeneration(IndependentId), State->IndependentGeneration);
        for (const FName Id : { GrowId, CloseId, ReplaceId, ReopenId, IndependentId }) Module.Remove(Id);
        for (int32 I = 0; I < 1024; ++I) Module.Remove(FName(*FString::Printf(TEXT("DockGrowthBinding%d"), I)));
        return true;
    }
private:
    FAutomationTestBase* Test; TSharedRef<FReentrantState> State;
};
}
IMPLEMENT_SIMPLE_AUTOMATION_TEST(FAuroraViewDockReentrancy, "AuroraView.Editor.DockFactoryReentrancy",
    EAutomationTestFlags::EditorContext | EAutomationTestFlags::EngineFilter)
bool FAuroraViewDockReentrancy::RunTest(const FString&)
{
    auto& Module = FModuleManager::LoadModuleChecked<FAuroraViewEditorModule>(TEXT("AuroraViewEditor"));
    for (const FName Id : { GrowId, CloseId, ReplaceId, ReopenId, IndependentId }) Module.Remove(Id);
    const auto State = MakeShared<FReentrantState>(); State->Deadline = FPlatformTime::Seconds() + 35;
    FString Error;
    TestTrue(TEXT("Independent browser opens"), Module.Open(IndependentId, Html, FText::FromName(IndependentId), Error));
    State->IndependentGeneration = Module.GetGeneration(IndependentId);
    Module.RegisterDocked(GrowId, Html, FText::FromName(GrowId), Error,
        [&Module](const TSharedRef<SDockTab>&, const TSharedRef<SWidget>& Browser)
        {
            for (int32 I = 0; I < 1024; ++I)
                Module.BindCall(FName(*FString::Printf(TEXT("DockGrowthBinding%d"), I)), TEXT("test.noop"),
                    [](const TSharedPtr<FJsonValue>& Params) { return FAuroraViewReply::Success(Params); });
            return Browser;
        });
    TestTrue(TEXT("Factory map growth preserves the copied session owner"), Module.OpenDocked(GrowId, Error));
    Module.RegisterDocked(CloseId, Html, FText::FromName(CloseId), Error,
        [&Module](const TSharedRef<SDockTab>&, const TSharedRef<SWidget>& Browser)
        { Module.Close(CloseId); return Browser; });
    TestFalse(TEXT("Self-close interrupts outer open"), Module.OpenDocked(CloseId, Error));
    Module.RegisterDocked(ReplaceId, Html, FText::FromName(ReplaceId), Error,
        [this, &Module](const TSharedRef<SDockTab>&, const TSharedRef<SWidget>& Browser)
        {
            Module.Remove(ReplaceId); FString ReplacementError;
            TestTrue(TEXT("Removed factory keeps its spawner until Slate finishes adoption"),
                FGlobalTabmanager::Get()->HasTabSpawner(FName(TEXT("AuroraView.View.DockFactoryReplace"))));
            TestTrue(TEXT("Removed ID can open an independent replacement"), Module.Open(ReplaceId, Html, FText::FromName(ReplaceId), ReplacementError));
            return Browser;
        });
    TestFalse(TEXT("Removed/replaced session cannot satisfy outer open"), Module.OpenDocked(ReplaceId, Error));
    TestFalse(TEXT("Removed spawner retires synchronously after Slate invocation"),
        FGlobalTabmanager::Get()->HasTabSpawner(FName(TEXT("AuroraView.View.DockFactoryReplace"))));
    Module.RegisterDocked(ReopenId, Html, FText::FromName(ReopenId), Error,
        [this, &Module](const TSharedRef<SDockTab>&, const TSharedRef<SWidget>& Browser)
        {
            Module.Close(ReopenId); FString ReopenError;
            TestTrue(TEXT("Same session can open a new generation"), Module.Open(ReopenId, Html, FText::FromName(ReopenId), ReopenError));
            return Browser;
        });
    TestFalse(TEXT("New generation cannot satisfy interrupted outer open"), Module.OpenDocked(ReopenId, Error));
    ADD_LATENT_AUTOMATION_COMMAND(FWaitForReentrantDock(this, State));
    return true;
}
#endif
