#include "AuroraViewEditorModule.h"
#include "AuroraViewEndpoint.h"
#include "AuroraViewWorkspace.h"
#include "Containers/Ticker.h"
#include "Dom/JsonObject.h"
#include "Framework/Application/SlateApplication.h"
#include "Framework/Docking/TabManager.h"
#include "HAL/IConsoleManager.h"
#include "HAL/PlatformTime.h"
#include "Interfaces/IPluginManager.h"
#include "IWebBrowserSingleton.h"
#include "IWebBrowserWindow.h"
#include "Misc/CoreDelegates.h"
#include "Misc/EngineVersion.h"
#include "Misc/FileHelper.h"
#include "Misc/Paths.h"
#include "Serialization/JsonReader.h"
#include "Serialization/JsonSerializer.h"
#include "SWebBrowser.h"
#include "UObject/StrongObjectPtr.h"
#include "WebBrowserModule.h"
#include "Widgets/SNullWidget.h"
#include "Widgets/SWindow.h"
#include "Widgets/Docking/SDockTab.h"
#include "Widgets/Text/STextBlock.h"
#include <atomic>

DEFINE_LOG_CATEGORY_STATIC(LogAuroraView, Log, All);

namespace
{
FString JsonText(const TSharedRef<FJsonObject>& Object)
{
    FString Result;
    const auto Writer = TJsonWriterFactory<>::Create(&Result);
    FJsonSerializer::Serialize(Object, Writer);
    // JSON is injected as a JavaScript expression, never as an HTML script tag.
    Result.ReplaceInline(TEXT("\u2028"), TEXT("\\u2028"));
    Result.ReplaceInline(TEXT("\u2029"), TEXT("\\u2029"));
    return Result;
}

FString JsString(const FString& Value)
{
    TSharedRef<FJsonObject> Object = MakeShared<FJsonObject>();
    Object->SetStringField(TEXT("value"), Value);
    return TEXT("(") + JsonText(Object) + TEXT(").value");
}

bool ReadAsset(const FString& Root, const FString& Relative, FString& Out)
{
    return !Root.IsEmpty() && FFileHelper::LoadFileToString(Out, *FPaths::Combine(Root, Relative));
}

// Only trusted local fragments are accepted. The CSP precedes all caller markup.
FString MakeDocument(const FString& Stub, const FString& Bootstrap, const FString& Fragment)
{
    return TEXT("<!doctype html><html><head><meta charset=\"utf-8\">")
        TEXT("<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; ")
        TEXT("script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; ")
        TEXT("connect-src 'none'; frame-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'\">")
        TEXT("<script>") + Stub + TEXT("\n") + Bootstrap
        + TEXT("</script></head><body>") + Fragment + TEXT("</body></html>");
}

struct FSession final : TSharedFromThis<FSession>
{
    std::shared_ptr<AuroraView::SessionMailbox> Mailbox = std::make_shared<AuroraView::SessionMailbox>();
    uint64 Generation = 0;
    uint64 PresentationGeneration = 0;
    FString Token;
    FString OwnedUrl;
    FString Bridge;
    FString Transport;
    TSharedPtr<IWebBrowserWindow> NativeBrowser;
    TSharedPtr<SWebBrowser> Browser;
    TSharedPtr<SWindow> Window;
    TWeakPtr<SDockTab> DockTab;
    FName DockId;
    FString DockFragment;
    FString LastOpenError;
    FText DockTitle;
    FAuroraViewDockContent ContentFactory;
    bool bDockRegistered = false;
    bool bDisposing = false;
    uint64 TabEpoch = 0, DockRequest = 0;

    void OwnDockTab(const TSharedRef<SDockTab>& Tab)
    {
        DockTab = Tab;
        const uint64 OwnedEpoch = ++TabEpoch;
        TWeakPtr<FSession> Weak = AsShared();
        // Install before browser construction: even an error tab has a lifetime.
        Tab->SetOnTabClosed(SDockTab::FOnTabClosedCallback::CreateLambda(
            [Weak, OwnedEpoch](TSharedRef<SDockTab> ClosedTab)
            {
                const auto Current = Weak.Pin();
                if (Current && Current->TabEpoch == OwnedEpoch && Current->DockTab.Pin() == ClosedTab)
                    Current->Dispose(false);
            }));
    }
#if WITH_DEV_AUTOMATION_TESTS
    bool bFailNextOpenForTesting = false;
#endif
    TStrongObjectPtr<UAuroraViewEndpoint> Endpoint;
    TMap<FString, FAuroraViewHandler> Handlers;
    TSet<FString> SeenIds;
    bool bReady = false;
    bool bBootAttempted = false;
    double ReadyDeadline = 0.0;

    void Emit(const FString& Event, const TSharedRef<FJsonObject>& Detail)
    {
        check(IsInGameThread());
        if (!Browser || !Mailbox->IsCurrent(Generation)) return;
        Browser->ExecuteJavascript(TEXT("if(window.auroraview){window.auroraview.trigger(")
            + JsString(Event) + TEXT(",") + JsonText(Detail) + TEXT(");}"));
    }

    void Reply(const FString& Id, const FAuroraViewReply& ReplyValue)
    {
        TSharedRef<FJsonObject> Envelope = MakeShared<FJsonObject>();
        Envelope->SetStringField(TEXT("id"), Id);
        Envelope->SetBoolField(TEXT("ok"), ReplyValue.bOk);
        if (ReplyValue.bOk)
            Envelope->SetField(TEXT("result"), ReplyValue.Result.IsValid()
                ? ReplyValue.Result : MakeShared<FJsonValueNull>());
        else
        {
            TSharedRef<FJsonObject> Error = MakeShared<FJsonObject>();
            Error->SetStringField(TEXT("name"), ReplyValue.ErrorName);
            Error->SetStringField(TEXT("message"), ReplyValue.ErrorMessage);
            Error->SetStringField(TEXT("code"), ReplyValue.ErrorCode);
            Envelope->SetObjectField(TEXT("error"), Error);
        }
        Emit(TEXT("__auroraview_call_result"), Envelope);
    }

    void Boot()
    {
        check(IsInGameThread());
        if (!Browser || bBootAttempted || Browser->GetUrl() != OwnedUrl || !Browser->IsLoaded()) return;
        bBootAttempted = true;
        // Wait for the asynchronous UObject binding before Core announces ready.
        // The code and timer live in the page and disappear with that browser.
        Browser->ExecuteJavascript(Transport + TEXT("\n(function(){var tries=0;function start(){")
            TEXT("if(window.__auroraviewInstallUETransport(") + JsString(Token) + TEXT(")){")
            + Bridge + TEXT("\n}else if(++tries<250){setTimeout(start,20);}")
            TEXT("else{console.error('AuroraView native binding unavailable');}}start();})();"));
    }

    void Handle(const FString& Payload)
    {
        check(IsInGameThread());
        TSharedPtr<FJsonObject> Message;
        if (!FJsonSerializer::Deserialize(TJsonReaderFactory<>::Create(Payload), Message) || !Message)
        {
            UE_LOG(LogAuroraView, Warning, TEXT("Rejected malformed bridge payload"));
            return;
        }
        FString Id;
        const bool bUsableId = Message->TryGetStringField(TEXT("id"), Id) && !Id.IsEmpty() && Id.Len() <= 256;
        // TryGetStringField permits numeric-to-string coercion in Unreal.
        // The wire discriminator must be a JSON string before conversion.
        const TSharedPtr<FJsonValue>* TypeValue = Message->Values.Find(TEXT("type"));
        const bool bStringType = TypeValue && TypeValue->IsValid() && (*TypeValue)->Type == EJson::String;
        const FString Type = bStringType ? (*TypeValue)->AsString() : FString();
        if (!bStringType || Type.IsEmpty())
        {
            if (bUsableId) Reply(Id, FAuroraViewReply::Failure(TEXT("InvalidRequestError"),
                TEXT("Request type must be a nonempty string"), TEXT("INVALID_REQUEST")));
            else UE_LOG(LogAuroraView, Warning, TEXT("Rejected request without a valid type or usable ID"));
            return;
        }
        if (Type == TEXT("event"))
        {
            // Ready uses the endpoint's reserved lifecycle slot instead of
            // competing with the replayed stub RPC queue.
            // General event callbacks are outside the minimum host contract.
            return;
        }
        if (!bUsableId) return;
        // Drop a repeated transport delivery; never repeat a host mutation.
        if (SeenIds.Contains(Id)) return;
        // Bound the replay ledger; do not evict and accidentally allow replay.
        if (SeenIds.Num() >= 8192)
        {
            Reply(Id, FAuroraViewReply::Failure(TEXT("SessionLimitError"),
                TEXT("Reopen this view to start a new request session"), TEXT("SESSION_LIMIT")));
            return;
        }
        SeenIds.Add(Id);
        FString Method;
        if (Type != TEXT("call") || !Message->TryGetStringField(TEXT("method"), Method) || Method.Len() > 256)
        {
            Reply(Id, FAuroraViewReply::Failure(TEXT("NotSupportedError"),
                TEXT("This host accepts auroraview.call only"), TEXT("UNSUPPORTED")));
            return;
        }
        const FAuroraViewHandler* Handler = Handlers.Find(Method);
        if (!Handler)
        {
            Reply(Id, FAuroraViewReply::Failure(TEXT("MethodNotFoundError"),
                TEXT("No registered host method: ") + Method, TEXT("METHOD_NOT_FOUND")));
            return;
        }
        const TSharedPtr<FJsonValue>* Params = Message->Values.Find(TEXT("params"));
        const uint64 StartedGeneration = Generation;
        // Copy allows the handler to rebind/unbind safely. No handler is invoked
        // while the mailbox mutex is held, and no cross-thread wait occurs.
        const FAuroraViewHandler Callable = *Handler;
        const FAuroraViewReply Result = Callable(Params ? *Params : MakeShared<FJsonValueNull>());
        if (Mailbox->IsCurrent(StartedGeneration)) Reply(Id, Result);
    }

    void Dispose(bool bDestroyWindow)
    {
        check(IsInGameThread());
        if (bDisposing) return;
        bDisposing = true;
        // Retire ownership before native destruction can call user code. Locals
        // hold this presentation only; a later open must not be torn down here.
        Mailbox->Close();
        ++TabEpoch; ++DockRequest;
        bReady = false; bBootAttempted = false; SeenIds.Reset();
        const auto OldBrowser = MoveTemp(Browser);
        const auto OldNativeBrowser = MoveTemp(NativeBrowser);
        const auto OldWindow = MoveTemp(Window);
        const auto OldTab = DockTab.Pin();
        DockTab.Reset();
        if (OldTab) OldTab->SetOnTabClosed(SDockTab::FOnTabClosedCallback());
        if (OldWindow) OldWindow->SetOnWindowClosed(FOnWindowClosed());
        if (OldBrowser)
        {
            // Best-effort Core notification after synchronous mailbox retirement.
            OldBrowser->ExecuteJavascript(TEXT("if(window.auroraview){window.auroraview.trigger('backend_error',{message:'connection lost: Unreal view closed'});}"));
            if (Endpoint.IsValid()) OldBrowser->UnbindUObject(TEXT("auroraview"), Endpoint.Get(), true);
            OldBrowser->StopLoad();
        }
        Endpoint.Reset();
        if (OldWindow) OldWindow->SetContent(SNullWidget::NullWidget);
        if (OldTab) OldTab->SetContent(SNullWidget::NullWidget);
        if (OldNativeBrowser) OldNativeBrowser->CloseBrowser(true, false);
        if (bDestroyWindow && OldTab && FSlateApplication::IsInitialized()) OldTab->RequestCloseTab();
        if (bDestroyWindow && OldWindow && FSlateApplication::IsInitialized())
            FSlateApplication::Get().RequestDestroyWindow(OldWindow.ToSharedRef());
        bDisposing = false;
    }

    void Pump()
    {
        check(IsInGameThread());
        for (auto& Message : Mailbox->Drain())
        {
            if (!Mailbox->IsCurrent(Message.Generation)) continue;
            switch (Message.Type)
            {
            case AuroraView::SessionMailbox::Kind::Loaded: Boot(); break;
            case AuroraView::SessionMailbox::Kind::LoadError:
                UE_LOG(LogAuroraView, Error, TEXT("Browser document load failed"));
                Dispose(true);
                break;
            case AuroraView::SessionMailbox::Kind::Ready:
                bReady = true;
                Mailbox->SetVisible(DockTab.IsValid() || (Window.IsValid() && Window->IsVisible()));
                UE_LOG(LogAuroraView, Display, TEXT("Bridge ready, generation %llu"), Generation);
                break;
            case AuroraView::SessionMailbox::Kind::Wire:
                Handle(UTF8_TO_TCHAR(Message.Payload.c_str()));
                break;
            }
        }
        if (Browser && !bReady && FPlatformTime::Seconds() > ReadyDeadline)
        {
            UE_LOG(LogAuroraView, Error, TEXT("AuroraView bridge startup timed out; closing failed view"));
            Dispose(true);
        }
    }
};
}

struct FAuroraViewEditorModule::FImpl
{
    TMap<FName, TSharedPtr<FSession>> Sessions;
    FTSTicker::FDelegateHandle TickHandle;
    FDelegateHandle ExitHandle;
    IConsoleObject* DemoCommand = nullptr;
    IConsoleObject* DockCommand = nullptr;
    bool bStopping = false;
    uint64 NextPresentationGeneration = 0;
    FString Root;

    bool IsCurrent(FName Id, const TSharedPtr<FSession>& Session) const
    {
        const auto* Found = Sessions.Find(Id);
        return !bStopping && Found && *Found == Session;
    }

    bool Tick(float)
    {
        check(IsInGameThread());
        if (bStopping) return false;
        // A handler may remove/open a view without invalidating map iteration.
        TArray<TSharedPtr<FSession>> Snapshot;
        Sessions.GenerateValueArray(Snapshot);
        for (const auto& Session : Snapshot) Session->Pump();
        return true;
    }
    void Stop()
    {
        check(IsInGameThread());
        if (bStopping) return;
        bStopping = true;
        FTSTicker::GetCoreTicker().RemoveTicker(TickHandle);
        TArray<TSharedPtr<FSession>> Snapshot;
        Sessions.GenerateValueArray(Snapshot);
        Sessions.Reset();
        for (const auto& Session : Snapshot)
        {
            if (Session->bDockRegistered)
                FGlobalTabmanager::Get()->UnregisterNomadTabSpawner(Session->DockId);
            Session->bDockRegistered = false;
            Session->Mailbox->Stop();
            Session->Dispose(true);
            Session->Handlers.Reset();
        }
    }
};

FAuroraViewReply FAuroraViewReply::Success(TSharedPtr<FJsonValue> Value)
{
    FAuroraViewReply Result;
    Result.Result = MoveTemp(Value);
    return Result;
}
FAuroraViewReply FAuroraViewReply::Failure(FString Name, FString Message, FString Code)
{
    FAuroraViewReply Result;
    Result.bOk = false;
    Result.ErrorName = MoveTemp(Name);
    Result.ErrorMessage = MoveTemp(Message);
    Result.ErrorCode = MoveTemp(Code);
    return Result;
}
FAuroraViewEditorModule::FAuroraViewEditorModule() = default;
FAuroraViewEditorModule::~FAuroraViewEditorModule() = default;

void FAuroraViewEditorModule::StartupModule()
{
    check(IsInGameThread());
    Impl = MakeUnique<FImpl>();
    const TSharedPtr<IPlugin> Plugin = IPluginManager::Get().FindPlugin(TEXT("AuroraView"));
    if (Plugin) Impl->Root = Plugin->GetBaseDir();
    Impl->TickHandle = FTSTicker::GetCoreTicker().AddTicker(
        FTickerDelegate::CreateRaw(Impl.Get(), &FImpl::Tick));
    Impl->ExitHandle = FCoreDelegates::OnEnginePreExit.AddRaw(Impl.Get(), &FImpl::Stop);
    Impl->DemoCommand = IConsoleManager::Get().RegisterConsoleCommand(
        TEXT("AuroraView.Demo"), TEXT("Open an AuroraView native Editor test view"),
        FConsoleCommandDelegate::CreateRaw(this, &FAuroraViewEditorModule::OpenDemo), ECVF_Default);
    FString DockHtml, DockError;
    const FName DockId(TEXT("NativeShowcase"));
    DockHtml = TEXT("<style>body{background:#171b24;color:#e8edf6;font:16px system-ui;padding:32px}")
        TEXT("button{padding:12px;border:0;border-radius:6px}</style><h1>AuroraView native workspace</h1>")
        TEXT("<p>The tabs, docking and layout are real Slate widgets. Selection and asset panels are unimplemented in stage 1.</p>")
        TEXT("<button onclick=\"auroraview.call('api.echo',{native:true}).then(v=>document.querySelector('pre').textContent=JSON.stringify(v))\">Run native echo</button><pre></pre>");
    if (!DockHtml.IsEmpty())
    {
        BindCall(DockId, TEXT("api.echo"), [](const TSharedPtr<FJsonValue>& Params)
            { return FAuroraViewReply::Success(Params); });
        RegisterDocked(DockId, DockHtml, FText::FromString(TEXT("AuroraView Native Showcase")), DockError,
            [](const TSharedRef<SDockTab>& Tab, const TSharedRef<SWidget>& Browser)
            { return SNew(SAuroraViewWorkspace).OwnerTab(Tab).Inspector(Browser); });
    }
    Impl->DockCommand = IConsoleManager::Get().RegisterConsoleCommand(
        TEXT("AuroraView.Showcase"), TEXT("Open the native docked acceptance workspace"),
        FConsoleCommandDelegate::CreateLambda([this, DockId]()
        {
            FString Error;
            if (!OpenDocked(DockId, Error)) UE_LOG(LogAuroraView, Error, TEXT("%s"), *Error);
        }), ECVF_Default);
}

void FAuroraViewEditorModule::ShutdownModule()
{
    check(IsInGameThread());
    if (!Impl) return;
    if (Impl->DockCommand) IConsoleManager::Get().UnregisterConsoleObject(Impl->DockCommand, false);
    if (Impl->DemoCommand) IConsoleManager::Get().UnregisterConsoleObject(Impl->DemoCommand, false);
    FCoreDelegates::OnEnginePreExit.Remove(Impl->ExitHandle);
    Impl->Stop();
    Impl.Reset();
}

bool FAuroraViewEditorModule::Open(FName Id, const FString& Fragment, const FText& Title, FString& OutError)
{
    return OpenPresentation(Id, Fragment, Title, OutError, nullptr);
}

bool FAuroraViewEditorModule::OpenPresentation(FName Id, const FString& Fragment, const FText& Title,
    FString& OutError, const TSharedPtr<SDockTab>& DockTab)
{
    check(IsInGameThread());
    OutError.Empty();
    if (!Impl || Impl->bStopping || Id.IsNone() || !FSlateApplication::IsInitialized())
    {
        OutError = TEXT("Editor/Slate host unavailable or stopping");
        return false;
    }
    TSharedPtr<FSession>& Slot = Impl->Sessions.FindOrAdd(Id);
    if (!Slot) Slot = MakeShared<FSession>();
    const TSharedPtr<FSession> Session = Slot;
    if (Session->bDisposing) { OutError = TEXT("View presentation is retiring"); return false; }
    if (Session->DockTab.Pin() != DockTab)
    { OutError = TEXT("Dock presentation is no longer current"); return false; }
    if (Session->Browser)
    {
        if (Session->DockTab.IsValid() != DockTab.IsValid())
        {
            OutError = TEXT("Close the existing view before changing its presentation");
            return false;
        }
        return Show(Id);
    }
#if WITH_DEV_AUTOMATION_TESTS
    if (Session->bFailNextOpenForTesting)
    {
        Session->bFailNextOpenForTesting = false;
        OutError = TEXT("Intentional native failed-spawn regression seam");
        return false;
    }
#endif
    FString Stub, Bootstrap;
    if (!ReadAsset(Impl->Root, TEXT("ThirdParty/AuroraViewCore/bridge_stub.js"), Stub)
        || !ReadAsset(Impl->Root, TEXT("ThirdParty/AuroraViewCore/event_bridge.js"), Session->Bridge)
        || !ReadAsset(Impl->Root, TEXT("Resources/ue_transport.js"), Session->Transport)
        || !ReadAsset(Impl->Root, TEXT("Resources/ue_bootstrap.js"), Bootstrap))
    {
        OutError = TEXT("Required pinned Core bridge/transport assets missing");
        return false;
    }
    IWebBrowserSingleton* Singleton = IWebBrowserModule::Get().GetSingleton();
    if (!Singleton)
    {
        OutError = TEXT("Engine WebBrowser backend unavailable");
        return false;
    }
    Session->Generation = Session->Mailbox->Open();
    if (!Session->Generation) { OutError = TEXT("View session cannot reopen"); return false; }
    Session->PresentationGeneration = ++Impl->NextPresentationGeneration;
    Session->Token = FGuid::NewGuid().ToString(EGuidFormats::Digits);
    // Unique document origin is separate from the endpoint token. This reduces
    // ambient same-origin sharing; storage/context isolation still needs CEF QA.
    Session->OwnedUrl = TEXT("https://") + FGuid::NewGuid().ToString(EGuidFormats::Digits)
        + TEXT(".auroraview.invalid/index.html");
    Session->ReadyDeadline = FPlatformTime::Seconds() + 10.0;
    FCreateBrowserWindowSettings Settings;
    Settings.InitialURL = TEXT("about:blank");
    Settings.bShowErrorMessage = false;
    Settings.bThumbMouseButtonNavigation = false;
    Session->NativeBrowser = Singleton->CreateBrowserWindow(Settings);
    if (!Session->NativeBrowser)
    {
        Session->Mailbox->Close();
        OutError = TEXT("Engine declined native browser creation");
        return false;
    }
    Session->Endpoint.Reset(NewObject<UAuroraViewEndpoint>(GetTransientPackage()));
    Session->Endpoint->Initialize(Session->Mailbox, Session->Generation, Session->Token);
    const auto Mailbox = Session->Mailbox;
    const uint64 Epoch = Session->Generation;
    const FString Url = Session->OwnedUrl;
    const auto NavigationUsed = std::make_shared<std::atomic<bool>>(false);
    Session->Browser = SNew(SWebBrowser, Session->NativeBrowser)
        .ShowControls(false)
        .ShowAddressBar(false)
        .OnBeforeNavigation_Lambda([Url, NavigationUsed](const FString& NewUrl, const FWebNavigationRequest& Request)
        {
            // No subframes, redirects, external pages, file URLs or arbitrary schemes.
            if (!Request.bIsMainFrame || Request.bIsRedirect || NewUrl != Url) return true;
            return NavigationUsed->exchange(true);
        })
        .OnBeforePopup_Lambda([](FString, FString) { return true; })
        .OnLoadCompleted_Lambda([Mailbox, Epoch]() { Mailbox->PushControl(Epoch, AuroraView::SessionMailbox::Kind::Loaded); })
        .OnLoadError_Lambda([Mailbox, Epoch]() { Mailbox->PushControl(Epoch, AuroraView::SessionMailbox::Kind::LoadError); });
    const auto Browser = Session->Browser;
    const auto NativeBrowser = Session->NativeBrowser;
    const auto Factory = Session->ContentFactory;
    const FString Document = MakeDocument(Stub, Bootstrap, Fragment);
    const FText PresentationTitle = Title;
    const uint64 PresentationEpoch = Session->TabEpoch;
    const auto IsCurrent = [this, Id, Session, Epoch, Browser, NativeBrowser, DockTab, PresentationEpoch]()
    {
        return Impl && Impl->IsCurrent(Id, Session) && !Session->bDisposing
            && Session->Generation == Epoch && Session->Mailbox->IsCurrent(Epoch)
            && Session->Browser == Browser && Session->NativeBrowser == NativeBrowser
            && Session->TabEpoch == PresentationEpoch && Session->DockTab.Pin() == DockTab;
    };
    Browser->BindUObject(TEXT("auroraview"), Session->Endpoint.Get(), true);
    if (!IsCurrent()) { OutError = TEXT("Native binding interrupted"); return false; }
    TWeakPtr<FSession> Weak = Session;
    if (DockTab)
    {
        const TSharedRef<SWidget> Content = Factory
            ? Factory(DockTab.ToSharedRef(), Browser.ToSharedRef())
            : StaticCastSharedRef<SWidget>(Browser.ToSharedRef());
        if (!IsCurrent()) { OutError = TEXT("Dock open interrupted by a retired or replaced presentation"); return false; }
        DockTab->SetContent(Content);
        if (!IsCurrent()) { OutError = TEXT("Dock content installation interrupted"); return false; }
        DockTab->SetLabel(PresentationTitle);
        // Cross-window activation/IME caches remain an installed-engine gate.
    }
    else
    {
        const auto Window = SNew(SWindow).Title(PresentationTitle).ClientSize(FVector2D(920, 640))
            [ Browser.ToSharedRef() ];
        Session->Window = Window;
        Browser->SetParentWindow(Window);
        Window->SetOnWindowClosed(FOnWindowClosed::CreateLambda([Weak, Epoch](const TSharedRef<SWindow>& ClosedWindow)
        {
            if (const auto Current = Weak.Pin(); Current && Current->Generation == Epoch && Current->Window == ClosedWindow)
                Current->Dispose(false);
        }));
        const auto Parent = FGlobalTabmanager::Get()->GetRootWindow();
        if (Parent) FSlateApplication::Get().AddWindowAsNativeChild(Window, Parent.ToSharedRef());
        else FSlateApplication::Get().AddWindow(Window);
        if (!IsCurrent() || Session->Window != Window)
        { OutError = TEXT("Window open interrupted by a retired or replaced presentation"); return false; }
    }
    if (!IsCurrent()) { OutError = TEXT("View open interrupted"); return false; }
    Browser->LoadString(Document, Url);
    if (!IsCurrent()) { OutError = TEXT("View load interrupted"); return false; }
    OutError.Empty();
    return true;
}

bool FAuroraViewEditorModule::RegisterDocked(FName Id, const FString& Fragment, const FText& Title,
    FString& OutError, FAuroraViewDockContent ContentFactory)
{
    check(IsInGameThread());
    OutError.Empty();
    if (!Impl || Impl->bStopping || Id.IsNone() || !FSlateApplication::IsInitialized())
    {
        OutError = TEXT("Editor/Slate host unavailable or stopping");
        return false;
    }
    auto& Slot = Impl->Sessions.FindOrAdd(Id);
    if (!Slot) Slot = MakeShared<FSession>();
    const auto SessionOwner = Slot;
    if (SessionOwner->bDisposing || SessionOwner->Browser || SessionOwner->DockTab.IsValid())
    {
        OutError = TEXT("Close this view before changing its dock registration");
        return false;
    }
    // Namespaced identity prevents collisions with other Editor tab spawners.
    const FName DockId(*(TEXT("AuroraView.View.") + Id.ToString()));
    if (SessionOwner->bDockRegistered)
    {
        FGlobalTabmanager::Get()->UnregisterNomadTabSpawner(SessionOwner->DockId);
        SessionOwner->bDockRegistered = false;
    }
    if (!SessionOwner->bDockRegistered)
    {
        TWeakPtr<FSession> Weak = SessionOwner;
        FGlobalTabmanager::Get()->RegisterNomadTabSpawner(DockId,
            FOnSpawnTab::CreateLambda([this, Id, Weak](const FSpawnTabArgs&)
            {
                const auto Tab = SNew(SDockTab).TabRole(ETabRole::NomadTab);
                const auto Session = Weak.Pin();
                // Track failed-spawn error tabs too so Close/Remove/shutdown can
                // retire them; they are not successful browser sessions.
                const bool bOwned = Session && Impl && Impl->IsCurrent(Id, Session);
                if (bOwned) { Session->OwnDockTab(Tab); Tab->SetLabel(Session->DockTitle); }
                FString Error;
                if (!bOwned || !OpenPresentation(Id, Session->DockFragment, Session->DockTitle, Error, Tab))
                {
                    if (Error.IsEmpty()) Error = TEXT("Dock session is no longer available");
                    if (Session && Impl && Impl->IsCurrent(Id, Session) && Session->DockTab.Pin() == Tab) Session->LastOpenError = Error;
                    UE_LOG(LogAuroraView, Warning, TEXT("Dock open failed: %s"), *Error);
                    Tab->SetContent(SNew(STextBlock).Text(FText::FromString(Error)));
                }
                else if (Impl && Impl->IsCurrent(Id, Session) && Session->DockTab.Pin() == Tab) Session->LastOpenError.Empty();
                return Tab;
            })).SetDisplayName(Title);
        SessionOwner->bDockRegistered = true;
        SessionOwner->DockId = DockId;
    }
    SessionOwner->DockFragment = Fragment;
    SessionOwner->DockTitle = Title;
    SessionOwner->ContentFactory = MoveTemp(ContentFactory);
    return true;
}

bool FAuroraViewEditorModule::OpenDocked(FName Id, FString& OutError)
{
    check(IsInGameThread());
    OutError.Empty();
    const auto* Entry = Impl && !Impl->bStopping ? Impl->Sessions.Find(Id) : nullptr;
    const TSharedPtr<FSession> Session = Entry ? *Entry : nullptr;
    if (!Session || !Session->bDockRegistered || Session->bDisposing)
    {
        OutError = TEXT("No docked view is registered for this ID"); return false;
    }
    if (Session->Window)
    {
        OutError = TEXT("Close the floating view before opening its dock"); return false;
    }
    const uint64 Request = ++Session->DockRequest;
    const auto Current = [this, Id, Session, Request]()
    { return Impl && Impl->IsCurrent(Id, Session) && Session->DockRequest == Request; };
    if (const auto ErrorTab = Session->DockTab.Pin(); ErrorTab && !Session->Browser)
    {
        const bool bOpened = OpenPresentation(Id, Session->DockFragment, Session->DockTitle, OutError, ErrorTab);
        if (!Current() || Session->DockTab.Pin() != ErrorTab)
        { OutError = TEXT("Dock retry interrupted by a retired presentation"); return false; }
        Session->LastOpenError = bOpened ? FString() : OutError;
        if (!bOpened) ErrorTab->SetContent(SNew(STextBlock).Text(FText::FromString(OutError)));
        return bOpened && Current() && Session->DockTab.Pin() == ErrorTab && Session->Browser.IsValid();
    }
    const FName DockId = Session->DockId;
    const auto Tab = FGlobalTabmanager::Get()->TryInvokeTab(DockId);
    if (!Current() || !Tab || Session->DockTab.Pin() != Tab)
    {
        // A spawner can finish registering its tab after its factory retired it.
        // Close that outer result only; never touch a replacement presentation.
        if (Tab && Session->DockTab.Pin() != Tab) Tab->RequestCloseTab();
        OutError = TEXT("Dock open interrupted by a retired or replaced presentation"); return false;
    }
    if (!Session->Browser)
    {
        OutError = Session->LastOpenError.IsEmpty()
            ? TEXT("Dock spawner could not create the view; see the Editor log") : Session->LastOpenError;
        return false;
    }
    return true;
}

#if WITH_DEV_AUTOMATION_TESTS
void FAuroraViewEditorModule::FailNextOpenForTesting(FName Id)
{
    check(IsInGameThread());
    if (Impl) if (auto* Session = Impl->Sessions.Find(Id)) (*Session)->bFailNextOpenForTesting = true;
}
#endif

bool FAuroraViewEditorModule::IsReady(FName Id) const
{
    check(IsInGameThread());
    const auto* Session = Impl ? Impl->Sessions.Find(Id) : nullptr;
    return Session && (*Session)->bReady && (*Session)->Browser.IsValid();
}
uint64 FAuroraViewEditorModule::GetGeneration(FName Id) const
{
    check(IsInGameThread());
    const auto* Session = Impl ? Impl->Sessions.Find(Id) : nullptr;
    return Session && (*Session)->Browser && (*Session)->Mailbox->IsCurrent((*Session)->Generation) ? (*Session)->PresentationGeneration : 0;
}
bool FAuroraViewEditorModule::EmitEvent(FName Id, const FString& Event, const TSharedRef<FJsonObject>& Detail)
{
    check(IsInGameThread());
    if (!IsReady(Id) || Event.IsEmpty()) return false;
    Impl->Sessions[Id]->Emit(Event, Detail);
    return true;
}

bool FAuroraViewEditorModule::Show(FName Id)
{
    check(IsInGameThread());
    const auto* Entry = Impl ? Impl->Sessions.Find(Id) : nullptr;
    const auto Session = Entry ? *Entry : TSharedPtr<FSession>();
    if (!Session || !Session->Browser) return false;
    const uint64 Epoch = Session->Generation;
    const auto Current = [this, Id, Session, Epoch]()
    { return Impl && Impl->IsCurrent(Id, Session) && Session->Mailbox->IsCurrent(Epoch); };
    if (const auto Tab = Session->DockTab.Pin())
    {
        Tab->ActivateInParent(ETabActivationCause::SetDirectly);
        if (!Current() || Session->DockTab.Pin() != Tab) return false;
    }
    else
    {
        const auto Window = Session->Window;
        if (!Window) return false;
        Window->ShowWindow();
        if (!Current() || Session->Window != Window) return false;
        Window->BringToFront();
        if (!Current() || Session->Window != Window) return false;
    }
    Session->Mailbox->SetVisible(true);
    return true;
}
bool FAuroraViewEditorModule::Hide(FName Id)
{
    check(IsInGameThread());
    const auto* Entry = Impl ? Impl->Sessions.Find(Id) : nullptr;
    const auto Session = Entry ? *Entry : TSharedPtr<FSession>();
    if (!Session || !Session->Window) return false;
    const auto Window = Session->Window;
    const uint64 Epoch = Session->Generation;
    Window->HideWindow();
    if (!Impl || !Impl->IsCurrent(Id, Session) || !Session->Mailbox->IsCurrent(Epoch) || Session->Window != Window) return false;
    Session->Mailbox->SetVisible(false);
    return true;
}
bool FAuroraViewEditorModule::Close(FName Id)
{
    check(IsInGameThread());
    const auto* Entry = Impl ? Impl->Sessions.Find(Id) : nullptr;
    const auto Session = Entry ? *Entry : TSharedPtr<FSession>();
    if (!Session) return false;
    Session->Dispose(true);
    return true;
}
bool FAuroraViewEditorModule::Remove(FName Id)
{
    check(IsInGameThread());
    const auto* Entry = Impl ? Impl->Sessions.Find(Id) : nullptr;
    const auto Session = Entry ? *Entry : TSharedPtr<FSession>();
    if (!Session) return false;
    // Remove map ownership before callback-capable destruction. A reentrant
    // registration gets an independent session that this removal never erases.
    Impl->Sessions.Remove(Id);
    if (Session->bDockRegistered)
        FGlobalTabmanager::Get()->UnregisterNomadTabSpawner(Session->DockId);
    Session->bDockRegistered = false;
    Session->Mailbox->Stop();
    Session->Dispose(true);
    return true;
}
bool FAuroraViewEditorModule::BindCall(FName Id, const FString& Method, FAuroraViewHandler Handler)
{
    check(IsInGameThread());
    if (!Impl || Impl->bStopping || Id.IsNone() || Method.IsEmpty() || !Handler) return false;
    auto& Session = Impl->Sessions.FindOrAdd(Id);
    if (!Session) Session = MakeShared<FSession>();
    Session->Handlers.Add(Method, MoveTemp(Handler));
    return true;
}
bool FAuroraViewEditorModule::UnbindCall(FName Id, const FString& Method)
{
    check(IsInGameThread());
    const auto* Session = Impl ? Impl->Sessions.Find(Id) : nullptr;
    return Session && (*Session)->Handlers.Remove(Method) > 0;
}
void FAuroraViewEditorModule::OpenDemo()
{
    check(IsInGameThread());
    if (!Impl || Impl->bStopping) return;
    const FName Id(TEXT("AuroraViewDemo"));
    BindCall(Id, TEXT("api.echo"), [](const TSharedPtr<FJsonValue>& Params) { return FAuroraViewReply::Success(Params); });
    BindCall(Id, TEXT("demo.error"), [](const TSharedPtr<FJsonValue>&)
    {
        return FAuroraViewReply::Failure(TEXT("DemoError"), TEXT("Expected native host error"));
    });
    BindCall(Id, TEXT("unreal.info"), [](const TSharedPtr<FJsonValue>&)
    {
        TSharedRef<FJsonObject> Info = MakeShared<FJsonObject>();
        Info->SetStringField(TEXT("engineVersion"), FEngineVersion::Current().ToString());
        Info->SetBoolField(TEXT("gameThread"), IsInGameThread());
        Info->SetStringField(TEXT("renderer"), TEXT("UE WebBrowser / Slate"));
        return FAuroraViewReply::Success(MakeShared<FJsonValueObject>(Info));
    });
    FString Html, Error;
    if (!ReadAsset(Impl->Root, TEXT("Resources/demo.html"), Html)
        || !Open(Id, Html, FText::FromString(TEXT("AuroraView · Unreal Editor")), Error))
        UE_LOG(LogAuroraView, Error, TEXT("Demo unavailable: %s"), *Error);
}

IMPLEMENT_MODULE(FAuroraViewEditorModule, AuroraViewEditor)
