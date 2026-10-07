#if WITH_DEV_AUTOMATION_TESTS
#include "AuroraViewFixture.h"
#include "AuroraViewNativeShowcase.h"
#include "Dom/JsonObject.h"
#include "Editor.h"
#include "Engine/Selection.h"
#include "GameFramework/Actor.h"
#include "Misc/AutomationTest.h"
#include "Modules/ModuleManager.h"

IMPLEMENT_SIMPLE_AUTOMATION_TEST(FAuroraViewFixtureTransform, "AuroraView.Showcase.FixtureTransform",
    EAutomationTestFlags::EditorContext | EAutomationTestFlags::EngineFilter)
bool FAuroraViewFixtureTransform::RunTest(const FString&)
{
    FString Error;
    TArray<TWeakObjectPtr<AActor>> Actors;
    if (!AuroraViewFixture::Create(Actors, Error)) { AddError(Error); return false; }
    TestEqual(TEXT("Exactly three isolated native fixture actors"), Actors.Num(), 3);
    AActor* Actor = Actors[0].Get();
    if (!IsValid(Actor)) { AddError(TEXT("Fixture actor is invalid")); return false; }
    auto& Module = FModuleManager::LoadModuleChecked<FAuroraViewEditorModule>(TEXT("AuroraViewEditor"));
    const auto Host = MakeShared<FAuroraViewNativeShowcase>(Module);
    GEditor->SelectNone(false, true, false); GEditor->SelectActor(Actor, true, true);
    const FTransform Before = Actor->GetActorTransform();
    const auto State = Host->Snapshot();
    const auto& Rows = State->GetArrayField(TEXT("actors"));
    if (Rows.Num() != 1) { AddError(TEXT("Expected exactly one actual selected fixture actor")); return false; }
    const auto Row = Rows[0]->AsObject();
    const auto Expected = Row->GetObjectField(TEXT("transform"));
    const auto Desired = MakeShared<FJsonObject>();
    Desired->Values = Expected->Values;
    const FVector NextLocation = Before.GetLocation() + FVector(75, 25, 10);
    Desired->SetArrayField(TEXT("location"), { MakeShared<FJsonValueNumber>(NextLocation.X),
        MakeShared<FJsonValueNumber>(NextLocation.Y), MakeShared<FJsonValueNumber>(NextLocation.Z) });
    const auto Params = MakeShared<FJsonObject>();
    Params->SetStringField(TEXT("actorId"), Row->GetStringField(TEXT("id")));
    Params->SetObjectField(TEXT("expected"), Expected);
    Params->SetObjectField(TEXT("transform"), Desired);
    const auto Edited = Host->Dispatch(TEXT("showcase.setTransform"), MakeShared<FJsonValueObject>(Params));
    TestTrue(TEXT("Validated transform is accepted"), Edited.bOk);
    if (!Edited.bOk) return false;
    TestTrue(TEXT("Actual native actor moved to requested location"), Actor->GetActorLocation().Equals(NextLocation, 0.0001));
    const auto Stale = Host->Dispatch(TEXT("showcase.setTransform"), MakeShared<FJsonValueObject>(Params));
    TestFalse(TEXT("Stale expected transform is rejected"), Stale.bOk);
    TestEqual(TEXT("Stale edit returns conflict"), Stale.ErrorCode, FString(TEXT("CONFLICT")));
    TestTrue(TEXT("Rejected stale edit leaves native actor unchanged"), Actor->GetActorLocation().Equals(NextLocation, 0.0001));
    const auto Restored = Host->Dispatch(TEXT("showcase.restoreTransform"), MakeShared<FJsonValueNull>());
    TestTrue(TEXT("Own last edit restores successfully"), Restored.bOk);
    if (!Restored.bOk) return false;
    TestTrue(TEXT("Actual native transform equals original"), Actor->GetActorTransform().Equals(Before, 0.0001));
    // The just-created restore transaction must be the top transaction here.
    GEditor->UndoTransaction();
    TestTrue(TEXT("Native Editor Undo reverses restore"), Actor->GetActorLocation().Equals(NextLocation, 0.0001));
    GEditor->RedoTransaction();
    TestTrue(TEXT("Native Editor Redo reapplies original transform"), Actor->GetActorTransform().Equals(Before, 0.0001));
    // Source test reports never stand in for actual CEF, mouse, dock or drag tests.
    return true;
}
#endif
