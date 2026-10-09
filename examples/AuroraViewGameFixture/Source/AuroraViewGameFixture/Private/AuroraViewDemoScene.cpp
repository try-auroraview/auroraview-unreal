#include "AuroraViewDemoScene.h"
#include "AuroraViewRuntimeModule.h"

#include "Camera/CameraComponent.h"
#include "Components/DirectionalLightComponent.h"
#include "Components/SceneComponent.h"
#include "Components/SkyLightComponent.h"
#include "Components/StaticMeshComponent.h"
#include "Components/TextRenderComponent.h"
#include "Engine/StaticMesh.h"
#include "Engine/TextureCube.h"
#include "Engine/World.h"
#include "GameFramework/PlayerController.h"
#include "Materials/MaterialInstanceDynamic.h"
#include "Modules/ModuleManager.h"
#include "Serialization/JsonSerializer.h"
#include "UObject/ConstructorHelpers.h"

#if WITH_EDITOR
#include "Editor.h"
#include "LevelEditorViewport.h"
#include "Runtime/Launch/Resources/Version.h"
#endif

namespace
{
    const float CubeRestHeight = 80.0f;
    const FVector CubeRestLocation(-170.0f, 0.0f, CubeRestHeight);

#if WITH_EDITOR
    void RequestEditorSceneRedraw(UWorld* World)
    {
        if (!GEditor || IsRunningCommandlet() || !World || World->WorldType != EWorldType::Editor)
        {
            return;
        }
#if ENGINE_MAJOR_VERSION == 4 && ENGINE_MINOR_VERSION < 26
        for (FLevelEditorViewportClient* Viewport : GEditor->LevelViewportClients)
#else
        for (FLevelEditorViewportClient* Viewport : GEditor->GetLevelViewportClients())
#endif
        {
            if (Viewport && Viewport->GetWorld() == World)
            {
                // Request the next normal draw, including moved-geometry hit proxies.
                // Realtime overrides, focus and Slate throttling remain engine-owned.
                Viewport->Invalidate(false, true);
            }
        }
    }
#endif

    void SetMeshColor(UStaticMeshComponent* Mesh, UMaterialInterface* Material,
        const FLinearColor& Color)
    {
        if (Mesh && Material)
        {
            UMaterialInstanceDynamic* Dynamic = Mesh->CreateDynamicMaterialInstance(0, Material);
            if (Dynamic)
            {
                Dynamic->SetVectorParameterValue(TEXT("Color"), Color);
            }
        }
    }

    void ConfigureText(UTextRenderComponent* Text, USceneComponent* Root,
        const FVector& Position, float Size, const FColor& Color, const TCHAR* Value)
    {
        Text->SetupAttachment(Root);
        Text->SetRelativeLocation(Position);
        Text->SetRelativeRotation((AAuroraViewDemoScene::DemoCameraLocation() - Position).Rotation());
        Text->SetHorizontalAlignment(EHTA_Center);
        Text->SetVerticalAlignment(EVRTA_TextCenter);
        Text->SetWorldSize(Size);
        Text->SetTextRenderColor(Color);
        Text->SetText(FText::FromString(Value));
        Text->SetCollisionEnabled(ECollisionEnabled::NoCollision);
    }
}

FVector AAuroraViewDemoScene::DemoCameraLocation()
{
    return FVector(-920.0f, -1100.0f, 720.0f);
}

FRotator AAuroraViewDemoScene::DemoCameraRotation()
{
    return (FVector(0.0f, 60.0f, 130.0f) - DemoCameraLocation()).Rotation();
}

AAuroraViewDemoScene::AAuroraViewDemoScene()
    : ShapeMaterial(nullptr), Revision(0)
{
    PrimaryActorTick.bCanEverTick = false;
    bFindCameraComponentWhenViewTarget = true;
    Tags.Add(TEXT("AuroraViewDemo"));

    SceneRoot = CreateDefaultSubobject<USceneComponent>(TEXT("SceneRoot"));
    SetRootComponent(SceneRoot);

    // Constructor references make these engine assets discoverable by the cooker.
    static ConstructorHelpers::FObjectFinder<UStaticMesh> CubeAsset(TEXT("/Engine/BasicShapes/Cube.Cube"));
    static ConstructorHelpers::FObjectFinder<UMaterialInterface> MaterialAsset(
        TEXT("/Engine/BasicShapes/BasicShapeMaterial.BasicShapeMaterial"));
    static ConstructorHelpers::FObjectFinder<UTextureCube> SkyAsset(
        TEXT("/Engine/MapTemplates/Sky/DaylightAmbientCubemap.DaylightAmbientCubemap"));
    ShapeMaterial = MaterialAsset.Object;

    CubeA = CreateDefaultSubobject<UStaticMeshComponent>(TEXT("CubeA"));
    CubeA->SetupAttachment(SceneRoot);
    CubeA->SetStaticMesh(CubeAsset.Object);
    CubeA->SetMaterial(0, ShapeMaterial);
    CubeA->SetRelativeLocation(CubeRestLocation);
    CubeA->SetRelativeScale3D(FVector(1.6f));
    CubeA->SetMobility(EComponentMobility::Movable);
    CubeA->SetCollisionEnabled(ECollisionEnabled::NoCollision);

    CubeB = CreateDefaultSubobject<UStaticMeshComponent>(TEXT("CubeB"));
    CubeB->SetupAttachment(SceneRoot);
    CubeB->SetStaticMesh(CubeAsset.Object);
    CubeB->SetMaterial(0, ShapeMaterial);
    CubeB->SetRelativeLocation(FVector(170.0f, 75.0f, 60.0f));
    CubeB->SetRelativeRotation(FRotator(0.0f, 18.0f, 0.0f));
    CubeB->SetRelativeScale3D(FVector(1.2f));
    CubeB->SetCollisionEnabled(ECollisionEnabled::NoCollision);

    Floor = CreateDefaultSubobject<UStaticMeshComponent>(TEXT("Floor"));
    Floor->SetupAttachment(SceneRoot);
    Floor->SetStaticMesh(CubeAsset.Object);
    Floor->SetMaterial(0, ShapeMaterial);
    Floor->SetRelativeLocation(FVector(0.0f, 0.0f, -15.0f));
    Floor->SetRelativeScale3D(FVector(12.5f, 11.0f, 0.3f));
    Floor->SetCollisionEnabled(ECollisionEnabled::NoCollision);

    Backdrop = CreateDefaultSubobject<UStaticMeshComponent>(TEXT("Backdrop"));
    Backdrop->SetupAttachment(SceneRoot);
    Backdrop->SetStaticMesh(CubeAsset.Object);
    Backdrop->SetMaterial(0, ShapeMaterial);
    Backdrop->SetRelativeLocation(FVector(0.0f, 510.0f, 260.0f));
    Backdrop->SetRelativeScale3D(FVector(12.5f, 0.3f, 5.5f));
    Backdrop->SetCollisionEnabled(ECollisionEnabled::NoCollision);

    KeyLight = CreateDefaultSubobject<UDirectionalLightComponent>(TEXT("KeyLight"));
    KeyLight->SetupAttachment(SceneRoot);
    KeyLight->SetRelativeRotation(FRotator(-48.0f, -25.0f, 0.0f));
    KeyLight->SetMobility(EComponentMobility::Movable);
    KeyLight->SetIntensity(3.0f);
    KeyLight->SetLightColor(FLinearColor(1.0f, 0.94f, 0.85f));

    FillLight = CreateDefaultSubobject<USkyLightComponent>(TEXT("FillLight"));
    FillLight->SetupAttachment(SceneRoot);
    FillLight->SetMobility(EComponentMobility::Movable);
    FillLight->SourceType = SLS_SpecifiedCubemap;
    FillLight->SetCubemap(SkyAsset.Object);
    FillLight->SetIntensity(0.8f);

    Camera = CreateDefaultSubobject<UCameraComponent>(TEXT("DemoCamera"));
    Camera->SetupAttachment(SceneRoot);
    Camera->SetRelativeLocation(DemoCameraLocation());
    Camera->SetRelativeRotation(DemoCameraRotation());
    Camera->SetFieldOfView(55.0f);
    Camera->AspectRatio = 16.0f / 9.0f;
    Camera->bConstrainAspectRatio = true;

    TitleText = CreateDefaultSubobject<UTextRenderComponent>(TEXT("TitleText"));
    ConfigureText(TitleText, SceneRoot, FVector(80.0f, 350.0f, 410.0f), 34.0f,
        FColor(225, 238, 255), TEXT("AURORAVIEW / UNREAL"));
    SubtitleText = CreateDefaultSubobject<UTextRenderComponent>(TEXT("SubtitleText"));
    ConfigureText(SubtitleText, SceneRoot, FVector(80.0f, 350.0f, 360.0f), 19.0f,
        FColor(150, 178, 200), TEXT("NATIVE SCENE + EXTERNAL PYTHON"));
    StatusText = CreateDefaultSubobject<UTextRenderComponent>(TEXT("StatusText"));
    ConfigureText(StatusText, SceneRoot, FVector(-50.0f, -340.0f, 55.0f), 22.0f,
        FColor(100, 235, 242), TEXT("CUBE A  /  LIFT 0 cm  /  REVISION 0"));
}

void AAuroraViewDemoScene::OnConstruction(const FTransform& Transform)
{
    Super::OnConstruction(Transform);
    ApplyColors();
    UpdateStatusText();
}

void AAuroraViewDemoScene::ApplyColors()
{
    SetMeshColor(CubeA, ShapeMaterial, FLinearColor(0.012f, 0.58f, 0.68f));
    SetMeshColor(CubeB, ShapeMaterial, FLinearColor(0.95f, 0.23f, 0.035f));
    SetMeshColor(Floor, ShapeMaterial, FLinearColor(0.035f, 0.05f, 0.075f));
    SetMeshColor(Backdrop, ShapeMaterial, FLinearColor(0.02f, 0.03f, 0.055f));
}

bool AAuroraViewDemoScene::SetCubeHeight(float Height)
{
    if (!CubeA || !FMath::IsFinite(Height) || Height < 0.0f || Height > 300.0f)
    {
        return false;
    }
    CubeA->SetRelativeLocation(CubeRestLocation + FVector(0.0f, 0.0f, Height));
    Revision = Revision == MAX_int32 ? 1 : Revision + 1;
    UpdateStatusText();
#if WITH_EDITOR
    RequestEditorSceneRedraw(GetWorld());
#endif
    return true;
}

FAuroraViewDemoState AAuroraViewDemoScene::ResetScene()
{
    SetCubeHeight(0.0f);
    return GetDemoState();
}

FAuroraViewDemoState AAuroraViewDemoScene::GetDemoState() const
{
    FAuroraViewDemoState State;
    if (CubeA)
    {
        State.Height = static_cast<float>(CubeA->GetRelativeTransform().GetLocation().Z) - CubeRestHeight;
        State.CubeLocation = CubeA->GetComponentLocation();
    }
    State.Revision = Revision;
    return State;
}

FString AAuroraViewDemoScene::GetAuroraViewState() const
{
    const auto Module = FModuleManager::GetModulePtr<FAuroraViewRuntimeModule>(TEXT("AuroraViewRuntime"));
    if (!Module) return TEXT("{\"exists\":false,\"ready\":false,\"reason\":\"runtime_not_loaded\"}");
    FString Result;
    FJsonSerializer::Serialize(Module->DescribeView(TEXT("LiveDemo")), TJsonWriterFactory<>::Create(&Result));
    return Result;
}

void AAuroraViewDemoScene::UpdateStatusText()
{
    if (StatusText)
    {
        const FAuroraViewDemoState State = GetDemoState();
        StatusText->SetText(FText::FromString(FString::Printf(
            TEXT("CUBE A  /  LIFT %.0f cm  /  REVISION %d"), State.Height, State.Revision)));
    }
}

AAuroraViewDemoGameMode::AAuroraViewDemoGameMode()
    : DemoScene(nullptr)
{
    DefaultPawnClass = nullptr;
    HUDClass = nullptr;
}

AAuroraViewDemoScene* AAuroraViewDemoGameMode::EnsureScene()
{
    if (!DemoScene && GetWorld())
    {
        FActorSpawnParameters Parameters;
        Parameters.Name = TEXT("AuroraViewDemoScene");
        DemoScene = GetWorld()->SpawnActor<AAuroraViewDemoScene>(
            AAuroraViewDemoScene::StaticClass(), FVector::ZeroVector, FRotator::ZeroRotator, Parameters);
    }
    return DemoScene;
}

void AAuroraViewDemoGameMode::ConfigurePlayer(APlayerController* Player)
{
    if (Player && EnsureScene())
    {
        Player->bAutoManageActiveCameraTarget = false;
        Player->SetViewTarget(DemoScene);
        Player->bShowMouseCursor = true;
    }
}

void AAuroraViewDemoGameMode::StartPlay()
{
    EnsureScene();
    Super::StartPlay();
    if (GetWorld())
    {
        for (FConstPlayerControllerIterator It = GetWorld()->GetPlayerControllerIterator(); It; ++It)
        {
            ConfigurePlayer(It->Get());
        }
    }
}

void AAuroraViewDemoGameMode::PostLogin(APlayerController* NewPlayer)
{
    Super::PostLogin(NewPlayer);
    ConfigurePlayer(NewPlayer);
}
