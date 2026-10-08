#include "AuroraViewDemoScene.h"

#include "Modules/ModuleManager.h"

#if WITH_EDITOR
#include "Editor.h"
#include "Engine/World.h"
#include "HAL/IConsoleManager.h"
#include "LevelEditorViewport.h"
#include "Misc/App.h"
#include "Misc/CommandLine.h"
#include "Misc/Parse.h"
#include "Runtime/Launch/Resources/Version.h"
#include "UObject/Package.h"
#endif

DEFINE_LOG_CATEGORY_STATIC(LogAuroraViewDemo, Log, All);

class FAuroraViewGameFixtureModule : public FDefaultGameModuleImpl
{
public:
    virtual void StartupModule() override
    {
#if WITH_EDITOR
        SceneCommand = IConsoleManager::Get().RegisterConsoleCommand(
            TEXT("AuroraView.Demo.Scene"),
            TEXT("Create the native scene in this isolated AuroraView demo Editor only."),
            FConsoleCommandDelegate::CreateRaw(this, &FAuroraViewGameFixtureModule::CreateEditorScene));
#endif
    }

    virtual void ShutdownModule() override
    {
#if WITH_EDITOR
        if (SceneCommand)
        {
            IConsoleManager::Get().UnregisterConsoleObject(SceneCommand, false);
            SceneCommand = nullptr;
        }
#endif
    }

private:
#if WITH_EDITOR
    void CreateEditorScene()
    {
        // Never create objects in another project, an existing authored map,
        // a PIE world, or a user's unsaved scene. The launcher owns this process.
        if (!GEditor || IsRunningCommandlet() || GEditor->PlayWorld
            || FCString::Strcmp(FApp::GetProjectName(), TEXT("AuroraViewGameFixture")) != 0
            || !FParse::Param(FCommandLine::Get(), TEXT("AuroraViewDemo")))
        {
            UE_LOG(LogAuroraViewDemo, Warning, TEXT("Demo scene requires the isolated project and -AuroraViewDemo."));
            return;
        }

        UWorld* World = GEditor->GetEditorWorldContext().World();
        if (!World || World->WorldType != EWorldType::Editor)
        {
            return;
        }
        // Repeated commands are idempotent for an already owned demo scene.
        if (OwnedEditorWorld.Get() == World && OwnedEditorScene.IsValid())
        {
            SetEditorView();
            return;
        }

        const FString PackageName = World->GetOutermost()->GetName();
        const bool bStartupMap = PackageName == TEXT("/Engine/Maps/Entry");
        const bool bUnsavedMap = PackageName.StartsWith(TEXT("/Temp/Untitled"))
            || PackageName.StartsWith(TEXT("/Engine/Transient"));
        if (World->GetOutermost()->IsDirty() || (!bStartupMap && !bUnsavedMap))
        {
            UE_LOG(LogAuroraViewDemo, Warning, TEXT("Refusing to replace a dirty or authored Editor map: %s"), *PackageName);
            return;
        }

        // GEditor creates a fresh unsaved world, so the engine Entry map itself
        // is never changed or saved by this demo.
        World = GEditor->NewMap();
        if (!World || World->WorldType != EWorldType::Editor)
        {
            return;
        }
        FActorSpawnParameters Parameters;
        Parameters.Name = TEXT("AuroraViewDemoScene");
        AAuroraViewDemoScene* Scene = World->SpawnActor<AAuroraViewDemoScene>(
            AAuroraViewDemoScene::StaticClass(), FVector::ZeroVector, FRotator::ZeroRotator, Parameters);
        if (Scene)
        {
            OwnedEditorWorld = World;
            OwnedEditorScene = Scene;
            Scene->SetActorLabel(TEXT("AuroraView Demo Scene"));
            SetEditorView();
            UE_LOG(LogAuroraViewDemo, Display, TEXT("AuroraView native demo scene is ready in an unsaved Editor world."));
        }
    }

    void SetEditorView()
    {
#if ENGINE_MAJOR_VERSION == 4 && ENGINE_MINOR_VERSION < 26
        for (FLevelEditorViewportClient* Viewport : GEditor->LevelViewportClients)
#else
        for (FLevelEditorViewportClient* Viewport : GEditor->GetLevelViewportClients())
#endif
        {
            if (Viewport && Viewport->IsPerspective())
            {
                Viewport->SetViewLocation(AAuroraViewDemoScene::DemoCameraLocation());
                Viewport->SetViewRotation(AAuroraViewDemoScene::DemoCameraRotation());
                Viewport->ViewFOV = 55.0f;
                Viewport->SetRealtime(true);
                Viewport->SetGameView(true);
                Viewport->Invalidate();
            }
        }
        GEditor->RedrawLevelEditingViewports();
    }

    IConsoleCommand* SceneCommand = nullptr;
    TWeakObjectPtr<UWorld> OwnedEditorWorld;
    TWeakObjectPtr<AAuroraViewDemoScene> OwnedEditorScene;
#endif
};

IMPLEMENT_PRIMARY_GAME_MODULE(FAuroraViewGameFixtureModule, AuroraViewGameFixture, "AuroraViewGameFixture");
