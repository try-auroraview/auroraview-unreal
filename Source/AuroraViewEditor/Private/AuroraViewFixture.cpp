#include "AuroraViewFixture.h"
#include "Components/StaticMeshComponent.h"
#include "Editor.h"
#include "Engine/StaticMesh.h"
#include "Engine/StaticMeshActor.h"
#include "Engine/World.h"
#include "Engine/Level.h"
#include "EngineUtils.h"
#include "Misc/App.h"
#include "Misc/CommandLine.h"
#include "Misc/Parse.h"
#include "ScopedTransaction.h"

namespace AuroraViewFixture
{
const FName FixtureTag(TEXT("AuroraViewAcceptanceV1"));
const FName RoleTags[] = { FName(TEXT("AuroraViewFixture.Role.A")), FName(TEXT("AuroraViewFixture.Role.B")), FName(TEXT("AuroraViewFixture.Role.Ground")) };
bool CheckGuard(FString& OutError)
{
    check(IsInGameThread());
    if (FString(FApp::GetProjectName()) != TEXT("AuroraViewNativeFixture")
        || !FParse::Param(FCommandLine::Get(), TEXT("AuroraViewAllowFixtureMutations")))
    {
        OutError = TEXT("Fixture mutations require the disposable AuroraViewNativeFixture project and -AuroraViewAllowFixtureMutations");
        return false;
    }
    if (!GEditor || GEditor->PlayWorld || !GEditor->GetEditorWorldContext().World())
    {
        OutError = TEXT("An idle Editor world is required; PIE is not allowed");
        return false;
    }
    if (GEditor->GetEditorWorldContext().World()->GetOutermost()->GetName().StartsWith(TEXT("/Engine/")))
    {
        OutError = TEXT("Create a new empty level in the disposable project first; engine maps are never edited");
        return false;
    }
    FString ExpectedMap;
    if (!FParse::Value(FCommandLine::Get(), TEXT("AuroraViewFixtureMap="), ExpectedMap)
        || !ExpectedMap.StartsWith(TEXT("/Game/AuroraViewAcceptance/"))
        || ExpectedMap.Contains(TEXT(".."))
        || GEditor->GetEditorWorldContext().World()->GetOutermost()->GetName() != ExpectedMap)
    {
        OutError = TEXT("Loaded map must exactly match explicit -AuroraViewFixtureMap=/Game/AuroraViewAcceptance/<SavedMap>; no mutation was attempted");
        return false;
    }
    OutError.Empty();
    return true;
}
bool Create(TArray<TWeakObjectPtr<AActor>>& OutActors, FString& OutError)
{
    OutActors.Reset();
    if (!CheckGuard(OutError)) return false;
    UWorld* World = GEditor->GetEditorWorldContext().World();
    // Reference an installed engine asset; no engine content is shipped here.
    UStaticMesh* Cube = LoadObject<UStaticMesh>(nullptr, TEXT("/Engine/BasicShapes/Cube.Cube"));
    if (!Cube)
    {
        OutError = TEXT("Installed built-in Cube asset is unavailable"); return false;
    }
    TArray<TWeakObjectPtr<AActor>> Roles;
    Roles.SetNum(3);
    int32 ExistingCount = 0;
    for (TActorIterator<AActor> It(World); It; ++It)
    {
        int32 Role = INDEX_NONE, RoleCount = 0;
        for (int32 I = 0; I < 3; ++I) if (It->ActorHasTag(RoleTags[I])) { Role = I; ++RoleCount; }
        if (!It->ActorHasTag(FixtureTag) && RoleCount == 0) continue;
        const auto MeshActor = Cast<AStaticMeshActor>(*It);
        if (!It->ActorHasTag(FixtureTag) || RoleCount != 1 || Roles[Role].IsValid()
            || !MeshActor || !MeshActor->GetStaticMeshComponent()
            || MeshActor->GetStaticMeshComponent()->GetStaticMesh() != Cube)
        {
            OutError = TEXT("Existing fixture has duplicate/substituted/missing role or mesh; use a fresh isolated map, nothing was overwritten");
            return false;
        }
        Roles[Role] = *It; ++ExistingCount;
    }
    if (ExistingCount != 0)
    {
        if (ExistingCount != 3)
        {
            OutError = TEXT("Partial fixture already exists; use a fresh disposable map instead of overwriting it"); return false;
        }
        // A, B, ground order is stable; user-modified transforms are preserved.
        OutActors = Roles;
        return true;
    }
    FScopedTransaction Transaction(FText::FromString(TEXT("Create AuroraView isolated acceptance fixture")));
    World->GetCurrentLevel()->Modify();
    const FVector Locations[] = { FVector(-150, 0, 100), FVector(150, 0, 100), FVector(0, 0, -30) };
    const FVector Scales[] = { FVector(1), FVector(1.4, 1, 1.6), FVector(9, 7, 0.2) };
    const TCHAR* Labels[] = { TEXT("AV_Fixture_Cube_A"), TEXT("AV_Fixture_Cube_B"), TEXT("AV_Fixture_Ground") };
    for (int32 I = 0; I < 3; ++I)
    {
        FActorSpawnParameters Params;
        Params.ObjectFlags |= RF_Transactional;
        AStaticMeshActor* Actor = World->SpawnActor<AStaticMeshActor>(Locations[I], FRotator::ZeroRotator, Params);
        if (!Actor)
        {
            for (const auto& Created : OutActors) if (Created.IsValid()) World->DestroyActor(Created.Get());
            OutActors.Reset(); Transaction.Cancel();
            OutError = TEXT("Host could not create the fixture actors");
            return false;
        }
        Actor->SetActorLabel(Labels[I]);
        Actor->Tags.Add(FixtureTag);
        Actor->Tags.Add(RoleTags[I]);
        Actor->GetStaticMeshComponent()->SetMobility(EComponentMobility::Movable);
        Actor->GetStaticMeshComponent()->SetStaticMesh(Cube);
        Actor->SetActorScale3D(Scales[I]);
        Actor->PostEditMove(true);
        OutActors.Add(Actor);
    }
    GEditor->SelectNone(false, true, false);
    GEditor->SelectActor(OutActors[0].Get(), true, true);
    GEditor->RedrawLevelEditingViewports();
    return true;
}
}
