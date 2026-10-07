#if WITH_DEV_AUTOMATION_TESTS
#include "AuroraViewFixture.h"
#include "AuroraViewNativeShowcase.h"
#include "Dom/JsonObject.h"
#include "Editor.h"
#include "Engine/Selection.h"
#include "Engine/World.h"
#include "Framework/Docking/TabManager.h"
#include "GameFramework/Actor.h"
#include "HAL/PlatformTime.h"
#include "Layout/Children.h"
#include "Misc/AutomationTest.h"
#include "Modules/ModuleManager.h"
#include "Widgets/Layout/SBox.h"
#include "Widgets/SNullWidget.h"

namespace
{
const FName InteractionId(TEXT("NativeInteractionGuards"));
struct FInteractionState
{
    TSharedPtr<FAuroraViewNativeShowcase> Host;
    TArray<TWeakObjectPtr<AActor>> Fixtures, PreviousSelection, TemporaryActors;
    TSharedPtr<SBox> RetainedClosed, RetainedRemoved;
    double Deadline = 0;
    int32 Phase = 0;
    uint64 ClosedBuildCount = 0;
};
bool IsEmpty(const TSharedPtr<SBox>& Container)
{
    return Container && Container->GetChildren()->Num() == 1
        && Container->GetChildren()->GetChildAt(0) == SNullWidget::NullWidget;
}
void SelectOnly(AActor* Actor)
{
    GEditor->SelectNone(false, true, false);
    if (IsValid(Actor)) GEditor->SelectActor(Actor, true, true);
}
class FWaitForNativeInteraction final : public IAutomationLatentCommand
{
public:
    FWaitForNativeInteraction(FAutomationTestBase* InTest, TSharedRef<FInteractionState> InState) : Test(InTest), State(InState) {}
    bool Update() override
    {
        auto& Module = FModuleManager::GetModuleChecked<FAuroraViewEditorModule>(TEXT("AuroraViewEditor"));
        if (FPlatformTime::Seconds() > State->Deadline)
        { Test->AddError(TEXT("Timed out waiting for native interaction guard fixture")); Cleanup(Module); return true; }
        if (State->Phase == 0)
        {
            if (!Module.IsReady(InteractionId)) return false;
            const auto Host = State->Host;
            const uint64 Generation = Module.GetGeneration(InteractionId);
            State->RetainedClosed = Host->GetOutlinerForTesting();
            Test->TestTrue(TEXT("Actual native Outliner exists before close"), State->RetainedClosed.IsValid() && !IsEmpty(State->RetainedClosed));
            FAuroraViewActorDragCapture Capture;
            // Guarded disposable native-world capacity regression. No drag operation
            // or attachment is started here; native destination/Undo remains separate.
            UWorld* FixtureWorld = State->Fixtures[0]->GetWorld();
            for (int32 I = 0; I < 129; ++I)
            {
                FActorSpawnParameters Params; Params.ObjectFlags |= RF_Transient;
                AActor* Actor = FixtureWorld->SpawnActor<AActor>(FVector::ZeroVector, FRotator::ZeroRotator, Params);
                if (!Actor) { Test->AddError(TEXT("Could not create capacity fixture")); Cleanup(Module); return true; }
                State->TemporaryActors.Add(Actor);
            }
            GEditor->SelectNone(false, true, false);
            for (int32 I = 0; I < 128; ++I) GEditor->SelectActor(State->TemporaryActors[I].Get(), true, I == 127);
            Test->TestTrue(TEXT("Exactly 128 actual native selected actors are admitted"), Host->PrepareActorDrag(Generation, Capture));
            Test->TestEqual(TEXT("128-actor payload has no omitted prefix/suffix"), Capture.Actors.Num(), 128);
            Test->TestTrue(TEXT("Unchanged 128-actor payload passes final admission"), Host->FinalizeActorDrag(Capture));
            GEditor->SelectActor(State->TemporaryActors[128].Get(), true, true);
            Test->TestFalse(TEXT("129 actual native selected actors reject the complete drag"), Host->PrepareActorDrag(Generation, Capture));
            Test->TestEqual(TEXT("Overflow capture retains all 129 for rejection rather than truncating"), Capture.Actors.Num(), 129);
            for (const auto& Actor : State->TemporaryActors)
                Test->TestTrue(TEXT("Rejected capacity attempt leaves actors unattached"), Actor.IsValid() && Actor->GetAttachParentActor() == nullptr);
            SelectOnly(State->Fixtures[0].Get());
            for (const auto& Actor : State->TemporaryActors) if (Actor.IsValid()) FixtureWorld->DestroyActor(Actor.Get());
            State->TemporaryActors.Reset();
            Test->TestTrue(TEXT("Complete actual fixture selection is admitted"), Host->PrepareActorDrag(Generation, Capture));
            Test->TestEqual(TEXT("Admitted payload contains the exact selected actor"), Capture.Actors.Num(), 1);
            if (Capture.Actors.Num() != 1) { Cleanup(Module); return true; }
            Test->TestTrue(TEXT("Valid unchanged payload passes final admission"), Host->FinalizeActorDrag(Capture));
            const bool bPrepared = Host->PrepareActorDrag(Generation, Capture);
            Test->TestTrue(TEXT("A second valid capture can be prepared"), bPrepared);
            if (!bPrepared || Capture.Actors.Num() != 1) { Cleanup(Module); return true; }
            Capture.Actors[0].Reset();
            Test->TestFalse(TEXT("Invalidated original weak payload cannot finalize"), Host->FinalizeActorDrag(Capture));
            const auto B = State->Fixtures[1];
            Host->SetActorDragFeedbackForTesting([B]() { SelectOnly(B.Get()); });
            Test->TestFalse(TEXT("Selection changed across feedback/refresh rejects all actors"), Host->PrepareActorDrag(Generation, Capture));
            SelectOnly(State->Fixtures[0].Get());
            Test->TestTrue(TEXT("Valid selection recovers after rejection"), Host->PrepareActorDrag(Generation, Capture));
            SelectOnly(State->Fixtures[1].Get());
            Test->TestFalse(TEXT("Selection changed after preparation cannot BeginDragDrop"), Host->FinalizeActorDrag(Capture));
            SelectOnly(State->Fixtures[0].Get());
            Host->SetActorDragFeedbackForTesting([&Module]() { Module.Close(InteractionId); });
            Test->TestFalse(TEXT("Close across feedback/refresh rejects retired generation"), Host->PrepareActorDrag(Generation, Capture));
            State->ClosedBuildCount = Host->GetOutlinerBuildCountForTesting();
            Host->Snapshot(); Host->Snapshot();
            Test->TestTrue(TEXT("Strongly retained closed Outliner container stays empty"), IsEmpty(State->RetainedClosed));
            Test->TestEqual(TEXT("Closed-generation refresh constructs no actor browser"), Host->GetOutlinerBuildCountForTesting(), State->ClosedBuildCount);
            State->Phase = 1;
            return false;
        }
        if (State->Phase == 1)
        {
            if (FGlobalTabmanager::Get()->FindExistingLiveTab(FName(TEXT("AuroraView.View.NativeInteractionGuards"))).IsValid()) return false;
            State->Host->Snapshot();
            Test->TestTrue(TEXT("Later closed tick leaves retained child empty"), IsEmpty(State->RetainedClosed));
            FString Error;
            if (!Module.OpenDocked(InteractionId, Error)) { Test->AddError(Error); Cleanup(Module); return true; }
            State->Phase = 2;
            return false;
        }
        if (!Module.IsReady(InteractionId)) return false;
        State->RetainedRemoved = State->Host->GetOutlinerForTesting();
        Test->TestTrue(TEXT("Reopen creates a live replacement Outliner container"), State->RetainedRemoved.IsValid() && State->RetainedRemoved != State->RetainedClosed);
        const uint64 BuildsBeforeRemove = State->Host->GetOutlinerBuildCountForTesting();
        Module.Remove(InteractionId);
        State->Host->Snapshot(); State->Host->Snapshot();
        Test->TestTrue(TEXT("Remove keeps every strongly retained native child empty"), IsEmpty(State->RetainedClosed) && IsEmpty(State->RetainedRemoved));
        Test->TestEqual(TEXT("Removed-generation refresh never constructs a browser"), State->Host->GetOutlinerBuildCountForTesting(), BuildsBeforeRemove);
        Cleanup(Module);
        return true;
    }
private:
    void Cleanup(FAuroraViewEditorModule& Module)
    {
        State->Host->Stop(); Module.Remove(InteractionId);
        GEditor->SelectNone(false, true, false);
        for (const auto& Actor : State->TemporaryActors)
            if (Actor.IsValid() && Actor->GetWorld()) Actor->GetWorld()->DestroyActor(Actor.Get());
        State->TemporaryActors.Reset();
        for (const auto& Actor : State->PreviousSelection) if (Actor.IsValid()) GEditor->SelectActor(Actor.Get(), true, true);
        State->RetainedClosed.Reset(); State->RetainedRemoved.Reset();
    }
    FAutomationTestBase* Test; TSharedRef<FInteractionState> State;
};
}
IMPLEMENT_SIMPLE_AUTOMATION_TEST(FAuroraViewNativeInteractionGuards, "AuroraView.Showcase.NativeInteractionGuards",
    EAutomationTestFlags::EditorContext | EAutomationTestFlags::EngineFilter)
bool FAuroraViewNativeInteractionGuards::RunTest(const FString&)
{
    FString Error;
    if (!AuroraViewFixture::CheckGuard(Error)) { AddError(Error); return false; }
    const auto State = MakeShared<FInteractionState>();
    for (FSelectionIterator It(*GEditor->GetSelectedActors()); It; ++It)
        if (AActor* Actor = Cast<AActor>(*It); IsValid(Actor)) State->PreviousSelection.Add(Actor);
    if (!AuroraViewFixture::Create(State->Fixtures, Error)) { AddError(Error); return false; }
    auto& Module = FModuleManager::LoadModuleChecked<FAuroraViewEditorModule>(TEXT("AuroraViewEditor"));
    Module.Remove(InteractionId);
    State->Host = MakeShared<FAuroraViewNativeShowcase>(Module, InteractionId);
    State->Deadline = FPlatformTime::Seconds() + 35;
    if (!State->Host->Start(TEXT("<h1>Actual native interaction guard fixture</h1>"), Error)
        || !Module.OpenDocked(InteractionId, Error))
    {
        AddError(Error); State->Host->Stop(); Module.Remove(InteractionId);
        GEditor->SelectNone(false, true, false);
        for (const auto& Actor : State->PreviousSelection) if (Actor.IsValid()) GEditor->SelectActor(Actor.Get(), true, true);
        return false;
    }
    ADD_LATENT_AUTOMATION_COMMAND(FWaitForNativeInteraction(this, State));
    return true;
}
#endif
