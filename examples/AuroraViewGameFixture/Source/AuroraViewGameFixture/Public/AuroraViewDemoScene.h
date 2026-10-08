#pragma once

#include "CoreMinimal.h"
#include "GameFramework/Actor.h"
#include "GameFramework/GameModeBase.h"
#include "AuroraViewDemoScene.generated.h"

class UCameraComponent;
class UDirectionalLightComponent;
class UMaterialInterface;
class USceneComponent;
class USkyLightComponent;
class UStaticMeshComponent;
class UTextRenderComponent;

/** Readback comes from the actual owned mesh, rather than the dashboard input. */
USTRUCT(BlueprintType)
struct AURORAVIEWGAMEFIXTURE_API FAuroraViewDemoState
{
    GENERATED_BODY()

    /** Vertical lift in centimetres above the initial resting position. */
    UPROPERTY(BlueprintReadOnly, Category = "AuroraView Demo")
    float Height = 0.0f;

    UPROPERTY(BlueprintReadOnly, Category = "AuroraView Demo")
    int32 Revision = 0;

    UPROPERTY(BlueprintReadOnly, Category = "AuroraView Demo")
    FVector CubeLocation = FVector::ZeroVector;
};

/** All scene geometry, lighting, labels and camera belong to this actor. */
UCLASS()
class AURORAVIEWGAMEFIXTURE_API AAuroraViewDemoScene : public AActor
{
    GENERATED_BODY()

public:
    AAuroraViewDemoScene();

    /** Accept finite lift offsets in [0, 300] cm. Invalid input changes nothing. */
    UFUNCTION(BlueprintCallable, Category = "AuroraView Demo")
    bool SetCubeHeight(float Height);

    UFUNCTION(BlueprintCallable, Category = "AuroraView Demo")
    FAuroraViewDemoState ResetScene();

    UFUNCTION(BlueprintPure, Category = "AuroraView Demo")
    FAuroraViewDemoState GetDemoState() const;

    virtual void OnConstruction(const FTransform& Transform) override;

    static FVector DemoCameraLocation();
    static FRotator DemoCameraRotation();

private:
    void ApplyColors();
    void UpdateStatusText();

    UPROPERTY()
    USceneComponent* SceneRoot;

    UPROPERTY(VisibleAnywhere, Category = "AuroraView Demo")
    UStaticMeshComponent* CubeA;

    UPROPERTY(VisibleAnywhere, Category = "AuroraView Demo")
    UStaticMeshComponent* CubeB;

    UPROPERTY()
    UStaticMeshComponent* Floor;

    UPROPERTY()
    UStaticMeshComponent* Backdrop;

    UPROPERTY()
    UCameraComponent* Camera;

    UPROPERTY()
    UDirectionalLightComponent* KeyLight;

    UPROPERTY()
    USkyLightComponent* FillLight;

    UPROPERTY()
    UTextRenderComponent* TitleText;

    UPROPERTY()
    UTextRenderComponent* SubtitleText;

    UPROPERTY()
    UTextRenderComponent* StatusText;

    UPROPERTY()
    UMaterialInterface* ShapeMaterial;

    UPROPERTY()
    int32 Revision;
};

/** Entry-map Game fixture with an explicit native scene and fixed camera. */
UCLASS()
class AURORAVIEWGAMEFIXTURE_API AAuroraViewDemoGameMode : public AGameModeBase
{
    GENERATED_BODY()

public:
    AAuroraViewDemoGameMode();

    virtual void StartPlay() override;
    virtual void PostLogin(APlayerController* NewPlayer) override;

private:
    AAuroraViewDemoScene* EnsureScene();
    void ConfigurePlayer(APlayerController* Player);

    UPROPERTY()
    AAuroraViewDemoScene* DemoScene;
};
