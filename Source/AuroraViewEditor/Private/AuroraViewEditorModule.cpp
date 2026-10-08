#include "AuroraViewEditorModule.h"
#include "Modules/ModuleManager.h"
#include "AuroraViewNativeShowcase.h"
#include "AuroraViewFixture.h"
#include "AuroraViewCompatibility.h"
#include "Editor.h"
#include "HAL/IConsoleManager.h"
#include "Interfaces/IPluginManager.h"
#include "Misc/FileHelper.h"
#include "Misc/Paths.h"
#include "Misc/CoreDelegates.h"
#include "Dom/JsonObject.h"
#include "Framework/Application/SlateApplication.h"
#include "Framework/Docking/TabManager.h"
#include "LevelEditor.h"
#include "Widgets/Docking/SDockTab.h"
#include "Widgets/SWindow.h"

namespace {
FAuroraViewRuntimeModule& Host() {
    return FModuleManager::LoadModuleChecked<FAuroraViewRuntimeModule>(TEXT("AuroraViewRuntime"));
}
FAuroraViewReply DockEditorView(const TSharedPtr<FJsonValue>& Params) {
    FString Id;
    const auto Args = Params.IsValid() && Params->Type == EJson::Object ? Params->AsObject() : nullptr;
    if (!Args.IsValid() || !Args->TryGetStringField(TEXT("id"), Id) || Id.IsEmpty() || Id.Len() > 128)
        return FAuroraViewReply::Failure(TEXT("InvalidParams"), TEXT("A view id is required"), TEXT("INVALID_PARAMS"));
    if (!GEditor || IsRunningCommandlet() || !FSlateApplication::IsInitialized())
        return FAuroraViewReply::Failure(TEXT("EditorUnavailable"), TEXT("An interactive Editor is required"), TEXT("EDITOR_UNAVAILABLE"));
    const auto Manager = FModuleManager::LoadModuleChecked<FLevelEditorModule>(TEXT("LevelEditor")).GetLevelEditorTabManager();
    const auto Root = FGlobalTabmanager::Get()->GetRootWindow();
    const auto Owner = Manager.IsValid() ? Manager->GetOwnerTab() : TSharedPtr<SDockTab>();
    if (!Root.IsValid() || !Root->GetNativeWindow().IsValid() || !Owner.IsValid() || Owner->GetParentWindow() != Root)
        return FAuroraViewReply::Failure(TEXT("EditorUnavailable"), TEXT("The Level Editor layout is not ready"), TEXT("EDITOR_UNAVAILABLE"));
    FName DestinationId(TEXT("LevelEditorSelectionDetails"));
    auto Destination = Manager->FindExistingLiveTab(DestinationId);
    if (!Destination.IsValid())
        Destination = AuroraViewCompatibility::TryInvokeTab(Manager.ToSharedRef(), DestinationId);
    if (!Destination.IsValid() || !Destination->GetParentWindow().IsValid())
        return FAuroraViewReply::Failure(TEXT("EditorUnavailable"), TEXT("The Details tab has not joined the Editor layout"), TEXT("EDITOR_UNAVAILABLE"));
    if (Destination->GetParentWindow() != Root)
    {
        // Respect a user's floating Details window. Only use an existing root
        // viewport as an alternative; never relocate another native tab.
        DestinationId = FName(TEXT("LevelEditorViewport"));
        Destination = Manager->FindExistingLiveTab(DestinationId);
    }
    if (!Destination.IsValid() || Destination->GetParentWindow() != Root)
        return FAuroraViewReply::Failure(TEXT("EditorUnavailable"), TEXT("No native destination is attached to the Editor root window"), TEXT("EDITOR_UNAVAILABLE"));
    FString Error;
    if (!Host().DockInTabManager(FName(*Id), Manager.ToSharedRef(), DestinationId, Error))
        return FAuroraViewReply::Failure(TEXT("DockFailed"), Error, TEXT("VIEW_DOCK_FAILED"));
    return FAuroraViewReply::Success(MakeShared<FJsonValueObject>(Host().DescribeView(FName(*Id))));
}
}
struct FAuroraViewEditorModule::FImpl {
    FDelegateHandle PostEngineInit;
    TSharedPtr<FAuroraViewNativeShowcase> Showcase;
    IConsoleObject* DockCommand = nullptr;
    IConsoleObject* FixtureCommand = nullptr;
};
FAuroraViewEditorModule::FAuroraViewEditorModule() = default;
FAuroraViewEditorModule::~FAuroraViewEditorModule() = default;
void FAuroraViewEditorModule::StartupModule() {
    Host();
    if (IsRunningCommandlet()) return;
    Impl = MakeUnique<FImpl>();
    // Default loading makes the preparation UCLASS available to UE4 commandlets.
    // Editor-only UI registration still waits until the engine is initialized.
    if (GEditor) InitializeShowcase();
    else Impl->PostEngineInit = AuroraViewCompatibility::PostEngineInit().AddRaw(this, &FAuroraViewEditorModule::InitializeShowcase);
}
void FAuroraViewEditorModule::InitializeShowcase() {
    if (!Impl || Impl->Showcase.IsValid()) return;
    if (!Host().RegisterTool(TEXT("editor.view.dock"), DockEditorView))
        UE_LOG(LogTemp, Error, TEXT("AuroraView could not register the Editor docking tool"));
    FString Html, Error;
    const auto Plugin = IPluginManager::Get().FindPlugin(TEXT("AuroraView"));
    Impl->Showcase = MakeShared<FAuroraViewNativeShowcase>(*this);
    if (!Plugin.IsValid() || !FFileHelper::LoadFileToString(Html, *FPaths::Combine(Plugin->GetBaseDir(), TEXT("Resources/native_showcase.html")))
        || !Impl->Showcase->Start(Html, Error))
        UE_LOG(LogTemp, Error, TEXT("AuroraView showcase registration failed: %s"), *Error);
    Impl->DockCommand = IConsoleManager::Get().RegisterConsoleCommand(TEXT("AuroraView.Showcase"),
        TEXT("Open the native docked AuroraView workspace"), FConsoleCommandDelegate::CreateLambda([this]() {
            FString Error; OpenDocked(FAuroraViewNativeShowcase::ViewId(), Error);
        }), ECVF_Default);
    Impl->FixtureCommand = IConsoleManager::Get().RegisterConsoleCommand(TEXT("AuroraView.Showcase.CreateFixture"),
        TEXT("Create actors in the isolated acceptance project"), FConsoleCommandDelegate::CreateLambda([]() {
            FString Error; TArray<TWeakObjectPtr<AActor>> Actors;
            if (!AuroraViewFixture::Create(Actors, Error)) UE_LOG(LogTemp, Error, TEXT("%s"), *Error);
        }), ECVF_Default);
}
void FAuroraViewEditorModule::ShutdownModule() {
    if (!Impl) return;
    Host().UnregisterTool(TEXT("editor.view.dock"));
    AuroraViewCompatibility::PostEngineInit().Remove(Impl->PostEngineInit);
    if (Impl->Showcase.IsValid()) Impl->Showcase->Stop();
    if (Impl->DockCommand) IConsoleManager::Get().UnregisterConsoleObject(Impl->DockCommand, false);
    if (Impl->FixtureCommand) IConsoleManager::Get().UnregisterConsoleObject(Impl->FixtureCommand, false);
    Impl.Reset();
}
bool FAuroraViewEditorModule::Open(FName Id, const FString& Html, const FText& Title, FString& Error) { return Host().Open(Id, Html, Title, Error); }
bool FAuroraViewEditorModule::RegisterDocked(FName Id, const FString& Html, const FText& Title, FString& Error, FAuroraViewDockContent Factory) { return Host().RegisterDocked(Id, Html, Title, Error, MoveTemp(Factory)); }
bool FAuroraViewEditorModule::OpenDocked(FName Id, FString& Error) { return Host().OpenDocked(Id, Error); }
#if WITH_DEV_AUTOMATION_TESTS
void FAuroraViewEditorModule::FailNextOpenForTesting(FName Id) { Host().FailNextOpenForTesting(Id); }
#endif
bool FAuroraViewEditorModule::IsReady(FName Id) const { return Host().IsReady(Id); }
uint64 FAuroraViewEditorModule::GetGeneration(FName Id) const { return Host().GetGeneration(Id); }
bool FAuroraViewEditorModule::EmitEvent(FName Id, const FString& Event, const TSharedRef<FJsonObject>& Detail) { return Host().EmitEvent(Id, Event, Detail); }
bool FAuroraViewEditorModule::Show(FName Id) { return Host().Show(Id); }
bool FAuroraViewEditorModule::Hide(FName Id) { return Host().Hide(Id); }
bool FAuroraViewEditorModule::Close(FName Id) { return Host().Close(Id); }
bool FAuroraViewEditorModule::Remove(FName Id) { return Host().Remove(Id); }
bool FAuroraViewEditorModule::BindCall(FName Id, const FString& Method, FAuroraViewHandler Handler) { return Host().BindCall(Id, Method, MoveTemp(Handler)); }
bool FAuroraViewEditorModule::UnbindCall(FName Id, const FString& Method) { return Host().UnbindCall(Id, Method); }
void FAuroraViewEditorModule::OpenDemo() { Host().OpenDemo(); }
IMPLEMENT_MODULE(FAuroraViewEditorModule, AuroraViewEditor)
