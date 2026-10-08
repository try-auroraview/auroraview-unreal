#include "AuroraViewEditorModule.h"
#include "Modules/ModuleManager.h"
#include "AuroraViewNativeShowcase.h"
#include "AuroraViewFixture.h"
#include "AuroraViewCompatibility.h"
#include "Editor.h"
#include "HAL/IConsoleManager.h"
#include "HAL/PlatformTime.h"
#include "Interfaces/IPluginManager.h"
#include "Misc/App.h"
#include "Misc/CommandLine.h"
#include "Misc/FileHelper.h"
#include "Misc/Parse.h"
#include "Misc/Paths.h"
#include "Misc/CoreDelegates.h"
#include "Dom/JsonObject.h"
#include "Framework/Application/SlateApplication.h"
#include "Framework/Docking/TabManager.h"
#include "Framework/MultiBox/MultiBoxBuilder.h"
#include "Framework/MultiBox/MultiBoxExtender.h"
#include "LevelEditor.h"
#include "Toolkits/AssetEditorToolkit.h"
#include "Widgets/Docking/SDockTab.h"
#include "Widgets/SWindow.h"

namespace {
const FName LiveDemoId(TEXT("LiveDemo"));
bool IsDemoProject() {
    return FCString::Strcmp(FApp::GetProjectName(), TEXT("AuroraViewGameFixture")) == 0
        && FParse::Param(FCommandLine::Get(), TEXT("AuroraViewDemo"));
}
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
    TSharedPtr<FExtender> DemoMenuExtender;
    TWeakPtr<FExtensibilityManager> DemoMenuManager;
    AuroraViewCompatibility::FTickerHandle DemoDockTick;
    uint64 DemoDockGeneration = 0;
    double DemoDockDeadline = 0;
    bool bDemoDockPending = false;

    bool HasDemo() const {
        return GEditor && IsDemoProject()
            && FModuleManager::Get().IsModuleLoaded(TEXT("AuroraViewGameFixture"))
            && Host().DescribeView(LiveDemoId)->GetBoolField(TEXT("dock_registered"));
    }
    bool CanReopenDemo() const { return !bDemoDockPending && HasDemo(); }
    void BuildDemoMenu(FMenuBuilder& Menu) {
        if (!HasDemo()) return;
        Menu.BeginSection(TEXT("AuroraViewDemo"), FText::FromString(TEXT("AuroraView")));
        Menu.AddMenuEntry(FText::FromString(TEXT("Reopen AuroraView Demo")),
            FText::FromString(TEXT("Reopen the registered demo panel and attach it to the default Editor stack.")),
            FSlateIcon(), FUIAction(FExecuteAction::CreateRaw(this, &FImpl::ReopenDemo),
                FCanExecuteAction::CreateRaw(this, &FImpl::CanReopenDemo)));
        Menu.EndSection();
    }
    bool TickDemoDock(float) {
        if (!bDemoDockPending) return false;
        if (!HasDemo() || Host().GetGeneration(LiveDemoId) != DemoDockGeneration) {
            bDemoDockPending = false;
            DemoDockTick.Reset();
            UE_LOG(LogTemp, Display, TEXT("AuroraView demo docking cancelled because its presentation was closed or replaced"));
            return false;
        }
        const auto Args = MakeShared<FJsonObject>();
        Args->SetStringField(TEXT("id"), LiveDemoId.ToString());
        const auto Reply = DockEditorView(MakeShared<FJsonValueObject>(Args));
        if (!Reply.bOk && Reply.ErrorCode == TEXT("EDITOR_UNAVAILABLE")
            && FPlatformTime::Seconds() < DemoDockDeadline) return true;
        bDemoDockPending = false;
        DemoDockTick.Reset();
        if (!Reply.bOk)
        {
            UE_LOG(LogTemp, Error, TEXT("AuroraView demo could not attach to the Editor root: %s: %s"),
                *Reply.ErrorCode, *Reply.ErrorMessage);
        }
        else
        {
            UE_LOG(LogTemp, Display, TEXT("AuroraView demo attached to the default Editor stack; generation %llu"),
                static_cast<unsigned long long>(DemoDockGeneration));
        }
        return false;
    }
    void ReopenDemo() {
        if (!CanReopenDemo()) return;
        FString Error;
        if (!Host().OpenDocked(LiveDemoId, Error)) {
            UE_LOG(LogTemp, Error, TEXT("AuroraView demo could not reopen: %s"), *Error);
            return;
        }
        DemoDockGeneration = Host().GetGeneration(LiveDemoId);
        DemoDockDeadline = FPlatformTime::Seconds() + 30.0;
        bDemoDockPending = true;
        // One bounded request uses the existing Core ticker. A close/replacement
        // cancels it, and repeated menu activation cannot create parallel work.
        if (TickDemoDock(0.0f))
            DemoDockTick = AuroraViewCompatibility::FTicker::GetCoreTicker().AddTicker(
                FTickerDelegate::CreateRaw(this, &FImpl::TickDemoDock), 0.1f);
    }
    void RegisterDemoMenu() {
        if (!IsDemoProject()) return;
        const auto Manager = FModuleManager::LoadModuleChecked<FLevelEditorModule>(TEXT("LevelEditor")).GetMenuExtensibilityManager();
        if (!Manager.IsValid()) return;
        DemoMenuExtender = MakeShared<FExtender>();
        DemoMenuExtender->AddMenuExtension(TEXT("WindowLayout"), EExtensionHook::Before, nullptr,
            FMenuExtensionDelegate::CreateRaw(this, &FImpl::BuildDemoMenu));
        Manager->AddExtender(DemoMenuExtender);
        DemoMenuManager = Manager;
    }
    void RemoveDemoMenu() {
        bDemoDockPending = false;
        AuroraViewCompatibility::FTicker::GetCoreTicker().RemoveTicker(DemoDockTick);
        DemoDockTick.Reset();
        const auto Manager = DemoMenuManager.Pin();
        if (Manager.IsValid() && DemoMenuExtender.IsValid()) Manager->RemoveExtender(DemoMenuExtender);
        DemoMenuExtender.Reset();
        DemoMenuManager.Reset();
    }
};
FAuroraViewEditorModule::FAuroraViewEditorModule() = default;
FAuroraViewEditorModule::~FAuroraViewEditorModule() = default;
void FAuroraViewEditorModule::StartupModule() {
    Host();
    if (IsRunningCommandlet()) return;
    Impl = MakeUnique<FImpl>();
    // Register before the UE4 main menu captures its combined extenders. The
    // entry itself is built only after the isolated demo has registered its view.
    Impl->RegisterDemoMenu();
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
    Impl->RemoveDemoMenu();
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
