#include "AuroraViewRuntimeModule.h"
#include "AuroraViewControlHost.h"
#include "AuroraViewCompatibility.h"
#include "AuroraViewEndpoint.h"
#include "BrowserDocumentStartup.h"
#include "Containers/Ticker.h"
#include "Dom/JsonObject.h"
#include "Framework/Application/SlateApplication.h"
#include "Framework/Docking/TabManager.h"
#include "GenericPlatform/GenericWindow.h"
#include "HAL/IConsoleManager.h"
#include "HAL/PlatformTime.h"
#include "Interfaces/IPluginManager.h"
#include "IWebBrowserSingleton.h"
#include "IWebBrowserWindow.h"
#include "Misc/CoreDelegates.h"
#include "Misc/EngineVersion.h"
#include "Misc/FileHelper.h"
#include "Misc/Paths.h"
#include "Misc/ScopeExit.h"
#include "Serialization/JsonReader.h"
#include "Serialization/JsonSerializer.h"
#include "SWebBrowser.h"
#include "UObject/StrongObjectPtr.h"
#include "UObject/Package.h"
#include "WebBrowserModule.h"
#include "Widgets/SNullWidget.h"
#include "Widgets/SWindow.h"
#include "Widgets/Docking/SDockTab.h"
#include "Widgets/Text/STextBlock.h"

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

// UE4 hides FindTabSpawnerFor. Track the ownership of our private tab namespace
// explicitly so delayed teardown can never unregister a replacement session.
TMap<FName, uint64> DockOwners;
uint64 NextDockOwner = 0;

class FExistingDockTarget final : public FTabManager::FSearchPreference
{
public:
    explicit FExistingDockTarget(const TSharedRef<SDockTab>& InTarget) : Target(InTarget) {}
    TSharedPtr<SDockTab> Search(const FTabManager&, FName, const TSharedRef<SDockTab>&) const override
    {
        return Target;
    }
private:
    TSharedRef<SDockTab> Target;
};

struct FSession final : TSharedFromThis<FSession>
{
    std::shared_ptr<AuroraView::SessionMailbox> Mailbox = std::make_shared<AuroraView::SessionMailbox>();
    uint64 Generation = 0;
    uint64 PresentationGeneration = 0;
    FString Token;
    FString OwnedUrl;
    FString Bridge;
    FString Transport;
    FString PendingDocument;
    std::shared_ptr<AuroraView::BrowserDocumentStartup> DocumentStartup;
    TSharedPtr<IWebBrowserWindow> NativeBrowser;
    TSharedPtr<SWebBrowser> Browser;
    TSharedPtr<SWindow> Window;
    TWeakPtr<SWindow> BrowserParentWindow;
    TWeakPtr<SDockTab> DockTab;
    FName DockId;
    FString DockFragment;
    FString LastOpenError;
    FText DockTitle;
    FAuroraViewDockContent ContentFactory;
    bool bDockRegistered = false;
    bool bDisposing = false;
    uint64 TabEpoch = 0, DockRequest = 0;
    TSharedPtr<FTabSpawnerEntry> DockSpawner;
    uint64 DockOwner = 0;
    int32 DockInvocationDepth = 0, DockFactoryDepth = 0;
    bool bDockCleanupScheduled = false;
    TFunction<void(const TSharedRef<FSession>&)> QueueDockCleanup;
    TArray<TFunction<void()>> PendingDockTeardown;

    void RetireDockSpawner()
    {
        if (!bDockRegistered) return;
        bDockRegistered = false;
        const auto Retire = [Id = DockId, Owner = DockOwner, Spawner = MoveTemp(DockSpawner)]()
        {
            const auto Manager = FGlobalTabmanager::Get();
            // A callback may have registered a replacement under the same ID.
            const auto* Current = DockOwners.Find(Id);
            if (!Current || *Current != Owner) return;
#if ENGINE_MAJOR_VERSION >= 5
            if (!Spawner.IsValid() || Manager->FindTabSpawnerFor(Id) != Spawner) return;
#endif
            Manager->UnregisterNomadTabSpawner(Id);
            DockOwners.Remove(Id);
        };
        if (DockInvocationDepth || DockFactoryDepth) PendingDockTeardown.Add(Retire);
        else Retire();
    }

    void FlushDockTeardown()
    {
        if (DockInvocationDepth || DockFactoryDepth) return;
        auto Teardown = MoveTemp(PendingDockTeardown);
        for (auto& Retire : Teardown) Retire();
    }

    void EndDockInvocation()
    {
        check(DockInvocationDepth > 0);
        --DockInvocationDepth;
        FlushDockTeardown();
    }

    void EndDockFactory()
    {
        check(DockFactoryDepth > 0);
        --DockFactoryDepth;
        if (DockInvocationDepth || DockFactoryDepth || PendingDockTeardown.Num() == 0 || bDockCleanupScheduled) return;
        // Layout restoration and native menus bypass OpenDocked. The module's
        // next core tick owns cleanup after Slate adopts the returned tab and
        // re-reads its spawner; shutdown also drains that retained session list.
        bDockCleanupScheduled = true;
        if (QueueDockCleanup) QueueDockCleanup(AsShared());
    }

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
                if (Current.IsValid() && Current->TabEpoch == OwnedEpoch && Current->DockTab.Pin() == ClosedTab)
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

    void LoadPendingDocument()
    {
        check(IsInGameThread());
        if (bDisposing || !Browser.IsValid() || !DocumentStartup || !Mailbox->IsCurrent(Generation)) return;
        const auto BrowserToLoad = Browser;
        // IsLoaded also covers initial completion before our delegate was bound.
        // Keep the opening deadline; a late first frame must never start a load.
        if (FPlatformTime::Seconds() > ReadyDeadline
            || !DocumentStartup->BeginOwnedDocument(BrowserToLoad->GetUrl() == TEXT("about:blank") && BrowserToLoad->IsLoaded())) return;
        const FString Document = MoveTemp(PendingDocument);
        const FString Url = OwnedUrl;
        BrowserToLoad->LoadString(Document, Url);
    }

    void Emit(const FString& Event, const TSharedRef<FJsonObject>& Detail)
    {
        check(IsInGameThread());
        if (!Browser.IsValid() || !Mailbox->IsCurrent(Generation)) return;
        Browser->ExecuteJavascript(TEXT("if(window.auroraview){window.auroraview.trigger(")
            + JsString(Event) + TEXT(",") + JsonText(Detail) + TEXT(");}"));
    }

    void EmitPayload(const FString& Event, const TSharedPtr<FJsonValue>& Detail)
    {
        check(IsInGameThread());
        if (!Browser.IsValid() || !Mailbox->IsCurrent(Generation)) return;
        auto Wrapper = MakeShared<FJsonObject>();
        Wrapper->SetField(TEXT("value"), Detail.IsValid() ? Detail : MakeShared<FJsonValueNull>());
        Browser->ExecuteJavascript(TEXT("if(window.auroraview){window.auroraview.trigger(")
            + JsString(Event) + TEXT(",(") + JsonText(Wrapper) + TEXT(").value);}"));
    }

    void Reply(const FString& Id, const FAuroraViewReply& ReplyValue, const FString& ResultEvent = TEXT("__auroraview_call_result"))
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
            if (ReplyValue.ErrorData.IsValid()) Error->SetField(TEXT("data"), ReplyValue.ErrorData);
            Envelope->SetObjectField(TEXT("error"), Error);
        }
        Emit(ResultEvent, Envelope);
    }

    void Boot()
    {
        check(IsInGameThread());
        if (!Browser.IsValid() || bBootAttempted || Browser->GetUrl() != OwnedUrl || !Browser->IsLoaded()) return;
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
        if (!FJsonSerializer::Deserialize(TJsonReaderFactory<>::Create(Payload), Message) || !Message.IsValid())
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
        const FString ReplyEvent = Type == TEXT("invoke") ? TEXT("__invoke_result__") : TEXT("__auroraview_call_result");
        if (Type == TEXT("event"))
        {
            FString Event;
            if (Message->TryGetStringField(TEXT("event"), Event) && !Event.StartsWith(TEXT("__auroraview_")))
            {
                const auto* Detail = Message->Values.Find(TEXT("detail"));
                auto* Host = FModuleManager::GetModulePtr<FAuroraViewRuntimeModule>(TEXT("AuroraViewRuntime"));
                if (Host) Host->BroadcastEvent(Event, Detail ? *Detail : MakeShared<FJsonValueNull>());
            }
            return;
        }
        if (!bUsableId) return;
        // Drop a repeated transport delivery; never repeat a host mutation.
        if (SeenIds.Contains(Id)) return;
        // Bound the replay ledger; do not evict and accidentally allow replay.
        if (SeenIds.Num() >= 8192)
        {
            Reply(Id, FAuroraViewReply::Failure(TEXT("SessionLimitError"),
                TEXT("Reopen this view to start a new request session"), TEXT("SESSION_LIMIT")), ReplyEvent);
            return;
        }
        SeenIds.Add(Id);
        FString Method;
        if ((Type != TEXT("call") && Type != TEXT("invoke"))
            || !Message->TryGetStringField(Type == TEXT("invoke") ? TEXT("cmd") : TEXT("method"), Method) || Method.Len() > 256)
        {
            Reply(Id, FAuroraViewReply::Failure(TEXT("NotSupportedError"),
                TEXT("Expected an AuroraView call or invoke envelope"), TEXT("UNSUPPORTED")), ReplyEvent);
            return;
        }
        const FAuroraViewHandler* Handler = Handlers.Find(Method);
        if (!Handler)
        {
            auto* Host = FModuleManager::GetModulePtr<FAuroraViewRuntimeModule>(TEXT("AuroraViewRuntime"));
            const auto* Params = Message->Values.Find(Type == TEXT("invoke") ? TEXT("args") : TEXT("params"));
            const uint64 StartedGeneration = Generation;
            TWeakPtr<FSession> Weak = AsShared();
            if (Host) Host->CallTool(Method, Params ? *Params : TSharedPtr<FJsonValue>(),
                [Weak, Id, StartedGeneration, ReplyEvent](FAuroraViewReply Result)
                {
                    const auto Session = Weak.Pin();
                    if (Session.IsValid() && Session->Mailbox->IsCurrent(StartedGeneration)) Session->Reply(Id, Result, ReplyEvent);
                });
            else Reply(Id, FAuroraViewReply::Failure(TEXT("HostStoppedError"), TEXT("AuroraView runtime is unavailable")), ReplyEvent);
            return;
        }
        const TSharedPtr<FJsonValue>* Params = Message->Values.Find(Type == TEXT("invoke") ? TEXT("args") : TEXT("params"));
        const uint64 StartedGeneration = Generation;
        // Copy allows the handler to rebind/unbind safely. No handler is invoked
        // while the mailbox mutex is held, and no cross-thread wait occurs.
        const FAuroraViewHandler Callable = *Handler;
        const FAuroraViewReply Result = Callable(Params ? *Params : MakeShared<FJsonValueNull>());
        if (Mailbox->IsCurrent(StartedGeneration)) Reply(Id, Result, ReplyEvent);
    }

    void Dispose(bool bDestroyWindow)
    {
        check(IsInGameThread());
        if (bDisposing) return;
        bDisposing = true;
        // Retire ownership before native destruction can call user code. Locals
        // hold this presentation only; a later open must not be torn down here.
        Mailbox->Close();
        if (DocumentStartup) DocumentStartup->Close();
        DocumentStartup.reset();
        PendingDocument.Empty();
        ++TabEpoch; ++DockRequest;
        bReady = false; bBootAttempted = false; SeenIds.Reset();
        const auto OldBrowser = MoveTemp(Browser);
        const auto OldNativeBrowser = MoveTemp(NativeBrowser);
        const auto OldWindow = MoveTemp(Window);
        BrowserParentWindow.Reset();
        const auto OldTab = DockTab.Pin();
        DockTab.Reset();
        if (OldTab.IsValid()) OldTab->SetOnTabClosed(SDockTab::FOnTabClosedCallback());
        if (OldWindow.IsValid()) OldWindow->SetOnWindowClosed(FOnWindowClosed());
        if (OldBrowser.IsValid())
        {
            // Best-effort Core notification after synchronous mailbox retirement.
            OldBrowser->ExecuteJavascript(TEXT("if(window.auroraview){window.auroraview.trigger('backend_error',{message:'connection lost: Unreal view closed'});}"));
            if (Endpoint.IsValid()) OldBrowser->UnbindUObject(TEXT("auroraview"), Endpoint.Get(), true);
            OldBrowser->StopLoad();
        }
        Endpoint.Reset();
        if (OldWindow.IsValid()) OldWindow->SetContent(SNullWidget::NullWidget);
        if (OldTab.IsValid()) OldTab->SetContent(SNullWidget::NullWidget);
        if (OldNativeBrowser.IsValid())
        {
#if ENGINE_MAJOR_VERSION >= 5
            OldNativeBrowser->CloseBrowser(true, false);
#else
            OldNativeBrowser->CloseBrowser(true);
#endif
        }
        if (bDestroyWindow && OldTab.IsValid() && FSlateApplication::IsInitialized())
        {
            // The spawner's tab is not adopted by Slate until TryInvokeTab
            // returns. Retire session ownership now, close that tab afterwards.
            const auto CloseTab = [OldTab]()
            { if (FSlateApplication::IsInitialized()) OldTab->RequestCloseTab(); };
            if (DockInvocationDepth || DockFactoryDepth) PendingDockTeardown.Add(CloseTab);
            else CloseTab();
        }
        if (bDestroyWindow && OldWindow.IsValid() && FSlateApplication::IsInitialized())
            FSlateApplication::Get().RequestDestroyWindow(OldWindow.ToSharedRef());
        bDisposing = false;
    }

    void Pump()
    {
        check(IsInGameThread());
        UpdateBrowserParent();
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
        if (Browser.IsValid() && !bReady && FPlatformTime::Seconds() > ReadyDeadline)
        {
            UE_LOG(LogAuroraView, Error, TEXT("AuroraView bridge startup timed out; closing failed view"));
            Dispose(true);
        }
        else LoadPendingDocument();
    }

    void UpdateBrowserParent()
    {
        if (!Browser.IsValid() || !NativeBrowser.IsValid()) return;
        const auto Tab = DockTab.Pin();
        const auto Parent = Tab.IsValid() ? AuroraViewCompatibility::FindTabWindow(Tab) : Window;
        if (!Parent.IsValid() || BrowserParentWindow.Pin() == Parent) return;
        // Slate adopts a spawned tab after its content factory returns. Follow
        // its actual host window on later ticks, including drag and redock.
        BrowserParentWindow = Parent;
#if ENGINE_MAJOR_VERSION >= 5
        Browser->SetParentWindow(Parent);
#else
        NativeBrowser->SetParentWindow(Parent);
#endif
    }
};
}

struct FAuroraViewRuntimeModule::FImpl
{
    TUniquePtr<FAuroraViewControlHost> ControlHost;
    TMap<FName, TSharedPtr<FSession>> Sessions;
    TArray<TSharedPtr<FSession>> PendingDockCleanup;
    AuroraViewCompatibility::FTickerHandle TickHandle;
    FDelegateHandle ExitHandle;
    IConsoleObject* DemoCommand = nullptr;
    bool bStopping = false;
    uint64 NextPresentationGeneration = 0;
    FString Root;

    TSharedRef<FSession> MakeSession()
    {
        const auto Session = MakeShared<FSession>();
        Session->QueueDockCleanup = [this](const TSharedRef<FSession>& Owner)
        { PendingDockCleanup.AddUnique(Owner); };
        return Session;
    }

    bool IsCurrent(FName Id, const TSharedPtr<FSession>& Session) const
    {
        const auto* Found = Sessions.Find(Id);
        return !bStopping && Found && *Found == Session;
    }

    bool Tick(float)
    {
        check(IsInGameThread());
        if (bStopping) return false;
        if (ControlHost) ControlHost->Tick();
        auto Retired = MoveTemp(PendingDockCleanup);
        for (const auto& Session : Retired)
        {
            if (Session->DockInvocationDepth || Session->DockFactoryDepth)
            { PendingDockCleanup.AddUnique(Session); continue; }
            Session->bDockCleanupScheduled = false;
            Session->FlushDockTeardown();
        }
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
        AuroraViewCompatibility::FTicker::GetCoreTicker().RemoveTicker(TickHandle);
        TArray<TSharedPtr<FSession>> Snapshot;
        Sessions.GenerateValueArray(Snapshot);
        for (const auto& Session : PendingDockCleanup) Snapshot.AddUnique(Session);
        PendingDockCleanup.Reset();
        Sessions.Reset();
        for (const auto& Session : Snapshot)
        {
            Session->QueueDockCleanup = {};
            Session->bDockCleanupScheduled = false;
            Session->RetireDockSpawner();
            Session->Mailbox->Stop();
            Session->Dispose(true);
            Session->FlushDockTeardown();
            Session->Handlers.Reset();
        }
        if (ControlHost) ControlHost->Stop();
    }
};

FAuroraViewReply FAuroraViewReply::Success(TSharedPtr<FJsonValue> Value)
{
    FAuroraViewReply Result;
    Result.Result = MoveTemp(Value);
    return Result;
}
FAuroraViewReply FAuroraViewReply::Failure(FString Name, FString Message, FString Code, TSharedPtr<FJsonValue> Data)
{
    FAuroraViewReply Result;
    Result.bOk = false;
    Result.ErrorName = MoveTemp(Name);
    Result.ErrorMessage = MoveTemp(Message);
    Result.ErrorCode = MoveTemp(Code);
    Result.ErrorData = MoveTemp(Data);
    return Result;
}
FAuroraViewRuntimeModule::FAuroraViewRuntimeModule() = default;
FAuroraViewRuntimeModule::~FAuroraViewRuntimeModule() = default;

void FAuroraViewRuntimeModule::StartupModule()
{
    check(IsInGameThread());
    Impl = MakeUnique<FImpl>();
    Impl->ControlHost = MakeUnique<FAuroraViewControlHost>(*this);
    Impl->ControlHost->Start();
    const TSharedPtr<IPlugin> Plugin = IPluginManager::Get().FindPlugin(TEXT("AuroraView"));
    if (Plugin.IsValid()) Impl->Root = Plugin->GetBaseDir();
    Impl->TickHandle = AuroraViewCompatibility::FTicker::GetCoreTicker().AddTicker(
        FTickerDelegate::CreateRaw(Impl.Get(), &FImpl::Tick));
    Impl->ExitHandle = AuroraViewCompatibility::PreExit().AddRaw(Impl.Get(), &FImpl::Stop);
    Impl->DemoCommand = IConsoleManager::Get().RegisterConsoleCommand(
        TEXT("AuroraView.Demo"), TEXT("Open an AuroraView native Editor test view"),
        FConsoleCommandDelegate::CreateRaw(this, &FAuroraViewRuntimeModule::OpenDemo), ECVF_Default);

}

void FAuroraViewRuntimeModule::ShutdownModule()
{
    check(IsInGameThread());
    if (!Impl) return;
    if (Impl->DemoCommand) IConsoleManager::Get().UnregisterConsoleObject(Impl->DemoCommand, false);
    AuroraViewCompatibility::PreExit().Remove(Impl->ExitHandle);
    Impl->Stop();
    Impl.Reset();
}

bool FAuroraViewRuntimeModule::Open(FName Id, const FString& Fragment, const FText& Title, FString& OutError)
{
    return OpenPresentation(Id, Fragment, Title, OutError, nullptr);
}

bool FAuroraViewRuntimeModule::OpenPresentation(FName Id, const FString& Fragment, const FText& Title,
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
    if (!Slot.IsValid()) Slot = Impl->MakeSession();
    const TSharedPtr<FSession> Session = Slot;
    if (Session->bDisposing) { OutError = TEXT("View presentation is retiring"); return false; }
    if (Session->DockTab.Pin() != DockTab)
    { OutError = TEXT("Dock presentation is no longer current"); return false; }
    if (Session->Browser.IsValid())
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
#if ENGINE_MAJOR_VERSION == 4
    // UE4's bundled CEF uses Chromium 59. These deterministic derivatives retain
    // the pinned Core protocol while lowering newer JavaScript syntax.
    const FString CoreRoot = TEXT("Resources/legacy/");
#else
    const FString CoreRoot = TEXT("ThirdParty/AuroraViewCore/");
#endif
    if (!ReadAsset(Impl->Root, CoreRoot + TEXT("bridge_stub.js"), Stub)
        || !ReadAsset(Impl->Root, CoreRoot + TEXT("event_bridge.js"), Session->Bridge)
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
    Session->DocumentStartup = std::make_shared<AuroraView::BrowserDocumentStartup>();
    Session->PendingDocument = MakeDocument(Stub, Bootstrap, Fragment);
    FCreateBrowserWindowSettings Settings;
    Settings.InitialURL = TEXT("about:blank");
    Settings.bShowErrorMessage = false;
    Settings.bThumbMouseButtonNavigation = false;
    Session->NativeBrowser = Singleton->CreateBrowserWindow(Settings);
    if (!Session->NativeBrowser.IsValid())
    {
        Session->Mailbox->Close();
        Session->DocumentStartup->Close();
        Session->DocumentStartup.reset();
        Session->PendingDocument.Empty();
        OutError = TEXT("Engine declined native browser creation");
        return false;
    }
    Session->Endpoint.Reset(NewObject<UAuroraViewEndpoint>(GetTransientPackage()));
    Session->Endpoint->Initialize(Session->Mailbox, Session->Generation, Session->Token);
    const auto Mailbox = Session->Mailbox;
    const uint64 Epoch = Session->Generation;
    const FString Url = Session->OwnedUrl;
    const auto DocumentStartup = Session->DocumentStartup;
    Session->Browser = SNew(SWebBrowser, Session->NativeBrowser)
        .ShowControls(false)
        .ShowAddressBar(false)
        .OnBeforeNavigation_Lambda([Url, DocumentStartup, Mailbox, Epoch](const FString& NewUrl, const FWebNavigationRequest& Request)
        {
            // Only the controlled initial blank and one owned document may load.
            using Document = AuroraView::BrowserDocumentStartup::Document;
            const auto Target = NewUrl == TEXT("about:blank") ? Document::Initial
                : (NewUrl == Url ? Document::Owned : Document::Other);
            return !Mailbox->IsCurrent(Epoch)
                || !DocumentStartup->AllowNavigation(Target, Request.bIsMainFrame, Request.bIsRedirect);
        })
        .OnBeforePopup_Lambda([](FString, FString) { return true; })
        .OnLoadCompleted_Lambda([Mailbox, Epoch]() { Mailbox->PushControl(Epoch, AuroraView::SessionMailbox::Kind::Loaded); })
        .OnLoadError_Lambda([Mailbox, Epoch]() { Mailbox->PushControl(Epoch, AuroraView::SessionMailbox::Kind::LoadError); });
    const auto Browser = Session->Browser;
    const auto NativeBrowser = Session->NativeBrowser;
    const auto Factory = Session->ContentFactory;
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
    if (DockTab.IsValid())
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
        Session->UpdateBrowserParent();
        Window->SetOnWindowClosed(FOnWindowClosed::CreateLambda([Weak, Epoch](const TSharedRef<SWindow>& ClosedWindow)
        {
            const auto Current = Weak.Pin();
            if (Current.IsValid() && Current->Generation == Epoch && Current->Window == ClosedWindow)
                Current->Dispose(false);
        }));
        const auto Parent = FGlobalTabmanager::Get()->GetRootWindow();
        if (Parent.IsValid()) FSlateApplication::Get().AddWindowAsNativeChild(Window, Parent.ToSharedRef());
        else FSlateApplication::Get().AddWindow(Window);
        if (!IsCurrent() || Session->Window != Window)
        { OutError = TEXT("Window open interrupted by a retired or replaced presentation"); return false; }
    }
    if (!IsCurrent()) { OutError = TEXT("View open interrupted"); return false; }
    // Pump loads the owned document exactly once after the initial frame completes.
    OutError.Empty();
    return true;
}

bool FAuroraViewRuntimeModule::RegisterDocked(FName Id, const FString& Fragment, const FText& Title,
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
    if (!Slot.IsValid()) Slot = Impl->MakeSession();
    const auto SessionOwner = Slot;
    if (SessionOwner->bDisposing || SessionOwner->Browser.IsValid() || SessionOwner->DockTab.IsValid())
    {
        OutError = TEXT("Close this view before changing its dock registration");
        return false;
    }
    // Namespaced identity prevents collisions with other Editor tab spawners.
    const FName DockId(*(TEXT("AuroraView.View.") + Id.ToString()));
    if (SessionOwner->bDockRegistered)
    {
        SessionOwner->RetireDockSpawner();
    }
    if (!SessionOwner->bDockRegistered)
    {
        TWeakPtr<FSession> Weak = SessionOwner;
        FGlobalTabmanager::Get()->RegisterNomadTabSpawner(DockId,
            FOnSpawnTab::CreateLambda([this, Id, Weak](const FSpawnTabArgs&)
            {
                const auto Tab = SNew(SDockTab).TabRole(ETabRole::NomadTab);
                const auto Session = Weak.Pin();
                if (Session.IsValid()) ++Session->DockFactoryDepth;
                ON_SCOPE_EXIT { if (Session.IsValid()) Session->EndDockFactory(); };
                // Track failed-spawn error tabs too so Close/Remove/shutdown can
                // retire them; they are not successful browser sessions.
                const bool bOwned = Session.IsValid() && Impl && Impl->IsCurrent(Id, Session);
                if (bOwned) { Session->OwnDockTab(Tab); Tab->SetLabel(Session->DockTitle); }
                FString Error;
                if (!bOwned || !OpenPresentation(Id, Session->DockFragment, Session->DockTitle, Error, Tab))
                {
                    if (Error.IsEmpty()) Error = TEXT("Dock session is no longer available");
                    if (Session.IsValid() && Impl && Impl->IsCurrent(Id, Session) && Session->DockTab.Pin() == Tab) Session->LastOpenError = Error;
                    UE_LOG(LogAuroraView, Warning, TEXT("Dock open failed: %s"), *Error);
                    Tab->SetContent(SNew(STextBlock).Text(FText::FromString(Error)));
                }
                else if (Impl && Impl->IsCurrent(Id, Session) && Session->DockTab.Pin() == Tab) Session->LastOpenError.Empty();
                return Tab;
            })).SetReuseTabMethod(FOnFindTabToReuse::CreateLambda([this, Id, Weak](const FTabId&)
            {
                // Slate's default weak cache can still pin a manually closed
                // tab retained by a caller. Reuse only this session's live tab.
                const auto Session = Weak.Pin();
                return Session.IsValid() && Impl && Impl->IsCurrent(Id, Session) && Session->bDockRegistered && !Session->bDisposing
                    ? Session->DockTab.Pin() : TSharedPtr<SDockTab>();
            })).SetDisplayName(Title);
        SessionOwner->bDockRegistered = true;
        SessionOwner->DockId = DockId;
        SessionOwner->DockOwner = ++NextDockOwner;
        DockOwners.Add(DockId, SessionOwner->DockOwner);
#if ENGINE_MAJOR_VERSION >= 5
        SessionOwner->DockSpawner = FGlobalTabmanager::Get()->FindTabSpawnerFor(DockId);
#endif
    }
    SessionOwner->DockFragment = Fragment;
    SessionOwner->DockTitle = Title;
    SessionOwner->ContentFactory = MoveTemp(ContentFactory);
    return true;
}

bool FAuroraViewRuntimeModule::OpenDocked(FName Id, FString& OutError)
{
    check(IsInGameThread());
    OutError.Empty();
    const auto* Entry = Impl && !Impl->bStopping ? Impl->Sessions.Find(Id) : nullptr;
    const TSharedPtr<FSession> Session = Entry ? *Entry : nullptr;
    if (!Session.IsValid() || !Session->bDockRegistered || Session->bDisposing)
    {
        OutError = TEXT("No docked view is registered for this ID"); return false;
    }
    if (Session->Window.IsValid())
    {
        OutError = TEXT("Close the floating view before opening its dock"); return false;
    }
    const uint64 Request = ++Session->DockRequest;
    const auto Current = [this, Id, Session, Request]()
    { return Impl && Impl->IsCurrent(Id, Session) && Session->DockRequest == Request; };
    const auto ErrorTab = Session->DockTab.Pin();
    if (ErrorTab.IsValid() && Session->Browser.IsValid())
    {
        // The tab may now belong to a Level Editor stack after a native move.
        // Invoking its global spawner again could relocate it into a new area.
        if (!Show(Id)) { OutError = TEXT("The current native tab could not be activated"); return false; }
        return Current() && Session->DockTab.Pin() == ErrorTab;
    }
    if (ErrorTab.IsValid() && !Session->Browser.IsValid())
    {
        const bool bOpened = OpenPresentation(Id, Session->DockFragment, Session->DockTitle, OutError, ErrorTab);
        if (!Current() || Session->DockTab.Pin() != ErrorTab)
        { OutError = TEXT("Dock retry interrupted by a retired presentation"); return false; }
        Session->LastOpenError = bOpened ? FString() : OutError;
        if (!bOpened) ErrorTab->SetContent(SNew(STextBlock).Text(FText::FromString(OutError)));
        return bOpened && Current() && Session->DockTab.Pin() == ErrorTab && Session->Browser.IsValid();
    }
    const FName DockId = Session->DockId;
    TSharedPtr<SDockTab> Tab;
    {
        // UE 5.7 RestoreArea_Helper looks up the spawner again after OnSpawnTab
        // returns. Keep its registration and unadopted tab alive for that entire
        // engine invocation, including reentrant Close/Remove from the factory.
        ++Session->DockInvocationDepth;
        ON_SCOPE_EXIT { Session->EndDockInvocation(); };
        Tab = AuroraViewCompatibility::TryInvokeTab(FGlobalTabmanager::Get(), DockId);
    }
    if (!Current() || !Tab.IsValid() || Session->DockTab.Pin() != Tab)
    {
        // A spawner can finish registering its tab after its factory retired it.
        // Close that outer result only; never touch a replacement presentation.
        if (Tab.IsValid() && Session->DockTab.Pin() != Tab) Tab->RequestCloseTab();
        OutError = TEXT("Dock open interrupted by a retired or replaced presentation"); return false;
    }
    if (!Session->Browser.IsValid())
    {
        OutError = Session->LastOpenError.IsEmpty()
            ? TEXT("Dock spawner could not create the view; see the Editor log") : Session->LastOpenError;
        return false;
    }
    return true;
}

bool FAuroraViewRuntimeModule::OpenDocked(FName Id, const FString& Fragment, const FText& Title, FString& OutError)
{
    check(IsInGameThread());
    OutError.Empty();
    if (!GIsEditor || IsRunningCommandlet())
    { OutError = TEXT("Native dock tabs require an interactive Editor host"); return false; }
    const auto* Entry = Impl && !Impl->bStopping ? Impl->Sessions.Find(Id) : nullptr;
    const auto Session = Entry ? *Entry : TSharedPtr<FSession>();
    if (Session.IsValid() && (Session->Browser.IsValid() || Session->DockTab.IsValid() || Session->Window.IsValid()))
    {
        if (!Session->bDockRegistered || Session->DockFragment != Fragment || !Session->DockTitle.EqualTo(Title))
        { OutError = TEXT("Close this view before changing its dock presentation or content"); return false; }
        return OpenDocked(Id, OutError);
    }
    return RegisterDocked(Id, Fragment, Title, OutError) && OpenDocked(Id, OutError);
}

bool FAuroraViewRuntimeModule::DockInTabManager(FName Id, const TSharedRef<FTabManager>& TargetManager,
    FName PlaceholderId, FString& OutError)
{
    check(IsInGameThread());
    OutError.Empty();
    if (!GIsEditor || IsRunningCommandlet() || !FSlateApplication::IsInitialized())
    { OutError = TEXT("Native dock tabs require an interactive Editor host"); return false; }
    const auto* Entry = Impl && !Impl->bStopping ? Impl->Sessions.Find(Id) : nullptr;
    const auto Session = Entry ? *Entry : TSharedPtr<FSession>();
    const auto Tab = Session.IsValid() ? Session->DockTab.Pin() : TSharedPtr<SDockTab>();
    if (!Session.IsValid() || Session->bDisposing || !Session->bDockRegistered || !Session->Browser.IsValid() || !Tab.IsValid())
    { OutError = TEXT("Open this registered native dock tab before moving it"); return false; }
    const auto Target = TargetManager->FindExistingLiveTab(PlaceholderId);
    const auto Root = FGlobalTabmanager::Get()->GetRootWindow();
    if (!Target.IsValid() || !Root.IsValid() || AuroraViewCompatibility::FindTabWindow(Target) != Root || Target == Tab)
    { OutError = TEXT("The destination must be an open tab in the Editor root window"); return false; }
    if (AuroraViewCompatibility::FindTabWindow(Tab) == Root)
    { Session->UpdateBrowserParent(); return Show(Id); }
    const uint64 Epoch = Session->Generation;
    const uint64 Request = ++Session->DockRequest;
    const auto Current = [this, Id, Session, Tab, Epoch, Request]()
    { return Impl && Impl->IsCurrent(Id, Session) && !Session->bDisposing && Session->DockTab.Pin() == Tab
        && Session->Mailbox->IsCurrent(Epoch) && Session->DockRequest == Request; };
    const FExistingDockTarget Search(Target.ToSharedRef());
    ++Session->DockInvocationDepth;
    ON_SCOPE_EXIT { Session->EndDockInvocation(); };
    // UE RemoveTabFromParent fires OnTabClosed even during a deliberate move.
    // Suspend only our lifecycle callback, then restore it for the adopted tab.
    Tab->SetOnTabClosed(SDockTab::FOnTabClosedCallback());
    Tab->RemoveTabFromParent();
    if (!Current()) { OutError = TEXT("Dock move interrupted while leaving its old stack"); return false; }
    // The insertion API assigns its first argument as the new tab type on UE4.
    // Use our own private type; Search selects the already-validated destination
    // stack independently. Slate owns the transient document instance ID, so
    // this explicit attachment does not overwrite the user's persisted layout.
    TargetManager->InsertNewDocumentTab(Session->DockId, Search, Tab.ToSharedRef());
    if (!Current()) { OutError = TEXT("Dock move interrupted by a retired presentation"); return false; }
    Session->OwnDockTab(Tab.ToSharedRef());
    Session->UpdateBrowserParent();
    if (!Current() || AuroraViewCompatibility::FindTabWindow(Tab) != Root || Tab->GetLayoutIdentifier().TabType != Session->DockId)
    {
        OutError = TEXT("Slate did not attach the native view to the requested Editor root stack");
        return false;
    }
    if (!Show(Id)) { OutError = TEXT("The attached native tab could not be activated"); return false; }
    return true;
}

#if WITH_DEV_AUTOMATION_TESTS
void FAuroraViewRuntimeModule::FailNextOpenForTesting(FName Id)
{
    check(IsInGameThread());
    if (Impl) if (auto* Session = Impl->Sessions.Find(Id)) (*Session)->bFailNextOpenForTesting = true;
}
#endif

bool FAuroraViewRuntimeModule::IsReady(FName Id) const
{
    check(IsInGameThread());
    const auto* Session = Impl ? Impl->Sessions.Find(Id) : nullptr;
    return Session && (*Session)->bReady && (*Session)->Browser.IsValid();
}
uint64 FAuroraViewRuntimeModule::GetGeneration(FName Id) const
{
    check(IsInGameThread());
    const auto* Session = Impl ? Impl->Sessions.Find(Id) : nullptr;
    return Session && (*Session)->Browser.IsValid() && (*Session)->Mailbox->IsCurrent((*Session)->Generation) ? (*Session)->PresentationGeneration : 0;
}
TSharedRef<FJsonObject> FAuroraViewRuntimeModule::DescribeView(FName Id) const
{
    check(IsInGameThread());
    const auto* Entry = Impl ? Impl->Sessions.Find(Id) : nullptr;
    const auto Session = Entry ? *Entry : TSharedPtr<FSession>();
    const auto Tab = Session.IsValid() ? Session->DockTab.Pin() : TSharedPtr<SDockTab>();
    const auto Window = Tab.IsValid() ? AuroraViewCompatibility::FindTabWindow(Tab) : (Session.IsValid() ? Session->Window : TSharedPtr<SWindow>());
    const auto Root = FSlateApplication::IsInitialized() ? FGlobalTabmanager::Get()->GetRootWindow() : TSharedPtr<SWindow>();
    const auto Handle = [](const TSharedPtr<SWindow>& Value)
    {
        const auto Native = Value.IsValid() ? Value->GetNativeWindow() : TSharedPtr<FGenericWindow>();
        return Native.IsValid() ? AuroraViewCompatibility::UInt64String(reinterpret_cast<UPTRINT>(Native->GetOSWindowHandle())) : FString();
    };
    auto State = MakeShared<FJsonObject>();
    State->SetStringField(TEXT("id"), Id.ToString());
    State->SetBoolField(TEXT("exists"), Session.IsValid());
    State->SetBoolField(TEXT("open"), GetGeneration(Id) != 0);
    State->SetBoolField(TEXT("ready"), IsReady(Id));
    State->SetNumberField(TEXT("generation"), GetGeneration(Id));
    State->SetStringField(TEXT("presentation"), Tab.IsValid() ? TEXT("docked") : (Window.IsValid()
        ? TEXT("floating") : (Session.IsValid() && Session->bDockRegistered ? TEXT("docked") : TEXT("none"))));
    State->SetBoolField(TEXT("dock_registered"), Session.IsValid() && Session->bDockRegistered);
    State->SetStringField(TEXT("tab_id"), Session.IsValid() && Session->bDockRegistered ? Session->DockId.ToString() : FString());
    State->SetStringField(TEXT("tab_layout_id"), Tab.IsValid() ? Tab->GetLayoutIdentifier().ToString() : FString());
    State->SetBoolField(TEXT("tab_open"), Tab.IsValid());
    State->SetBoolField(TEXT("tab_active"), Tab.IsValid() && Tab->IsActive());
    State->SetBoolField(TEXT("tab_foreground"), Tab.IsValid() && Tab->IsForeground());
    State->SetBoolField(TEXT("attached_to_root_window"), Tab.IsValid() && Window.IsValid() && Root.IsValid() && Window == Root);
    State->SetStringField(TEXT("window_native_handle"), Handle(Window));
    State->SetStringField(TEXT("root_window_native_handle"), Handle(Root));
    State->SetStringField(TEXT("browser_parent_window_native_handle"), Handle(Session.IsValid() ? Session->BrowserParentWindow.Pin() : TSharedPtr<SWindow>()));
    State->SetBoolField(TEXT("window_visible"), Window.IsValid() && Window->IsVisible());
    State->SetBoolField(TEXT("window_minimized"), Window.IsValid() && Window->IsWindowMinimized());
    State->SetBoolField(TEXT("window_maximized"), Window.IsValid() && Window->IsWindowMaximized());
    auto Geometry = MakeShared<FJsonObject>();
    Geometry->SetStringField(TEXT("coordinate_space"), TEXT("slate_absolute"));
    if (Session.IsValid() && Session->Browser.IsValid())
    {
        const auto& NativeGeometry = Session->Browser->GetCachedGeometry();
        Geometry->SetNumberField(TEXT("x"), NativeGeometry.GetAbsolutePosition().X);
        Geometry->SetNumberField(TEXT("y"), NativeGeometry.GetAbsolutePosition().Y);
        Geometry->SetNumberField(TEXT("width"), NativeGeometry.GetLocalSize().X);
        Geometry->SetNumberField(TEXT("height"), NativeGeometry.GetLocalSize().Y);
    }
    State->SetObjectField(TEXT("browser_geometry"), Geometry);
    return State;
}
bool FAuroraViewRuntimeModule::EmitEvent(FName Id, const FString& Event, const TSharedRef<FJsonObject>& Detail)
{
    check(IsInGameThread());
    if (!IsReady(Id) || Event.IsEmpty()) return false;
    Impl->Sessions[Id]->Emit(Event, Detail);
    return true;
}

bool FAuroraViewRuntimeModule::Show(FName Id)
{
    check(IsInGameThread());
    const auto* Entry = Impl ? Impl->Sessions.Find(Id) : nullptr;
    const auto Session = Entry ? *Entry : TSharedPtr<FSession>();
    if (!Session.IsValid() || !Session->Browser.IsValid()) return false;
    const uint64 Epoch = Session->Generation;
    const auto Current = [this, Id, Session, Epoch]()
    { return Impl && Impl->IsCurrent(Id, Session) && Session->Mailbox->IsCurrent(Epoch); };
    const auto Tab = Session->DockTab.Pin();
    if (Tab.IsValid())
    {
        Tab->ActivateInParent(ETabActivationCause::SetDirectly);
        if (!Current() || Session->DockTab.Pin() != Tab) return false;
    }
    else
    {
        const auto Window = Session->Window;
        if (!Window.IsValid()) return false;
        Window->ShowWindow();
        if (!Current() || Session->Window != Window) return false;
        Window->BringToFront();
        if (!Current() || Session->Window != Window) return false;
    }
    Session->Mailbox->SetVisible(true);
    return true;
}
bool FAuroraViewRuntimeModule::Hide(FName Id)
{
    check(IsInGameThread());
    const auto* Entry = Impl ? Impl->Sessions.Find(Id) : nullptr;
    const auto Session = Entry ? *Entry : TSharedPtr<FSession>();
    if (!Session.IsValid() || !Session->Window.IsValid()) return false;
    const auto Window = Session->Window;
    const uint64 Epoch = Session->Generation;
    Window->HideWindow();
    if (!Impl || !Impl->IsCurrent(Id, Session) || !Session->Mailbox->IsCurrent(Epoch) || Session->Window != Window) return false;
    Session->Mailbox->SetVisible(false);
    return true;
}
bool FAuroraViewRuntimeModule::Close(FName Id)
{
    check(IsInGameThread());
    const auto* Entry = Impl ? Impl->Sessions.Find(Id) : nullptr;
    const auto Session = Entry ? *Entry : TSharedPtr<FSession>();
    if (!Session.IsValid()) return false;
    Session->Dispose(true);
    return true;
}
bool FAuroraViewRuntimeModule::Remove(FName Id)
{
    check(IsInGameThread());
    const auto* Entry = Impl ? Impl->Sessions.Find(Id) : nullptr;
    const auto Session = Entry ? *Entry : TSharedPtr<FSession>();
    if (!Session.IsValid()) return false;
    // Remove map ownership before callback-capable destruction. A reentrant
    // registration gets an independent session that this removal never erases.
    Impl->Sessions.Remove(Id);
    Session->RetireDockSpawner();
    Session->Mailbox->Stop();
    Session->Dispose(true);
    return true;
}
bool FAuroraViewRuntimeModule::BindCall(FName Id, const FString& Method, FAuroraViewHandler Handler)
{
    check(IsInGameThread());
    if (!Impl || Impl->bStopping || Id.IsNone() || Method.IsEmpty() || !Handler) return false;
    auto& Session = Impl->Sessions.FindOrAdd(Id);
    if (!Session.IsValid()) Session = Impl->MakeSession();
    Session->Handlers.Add(Method, MoveTemp(Handler));
    return true;
}
bool FAuroraViewRuntimeModule::UnbindCall(FName Id, const FString& Method)
{
    check(IsInGameThread());
    const auto* Session = Impl ? Impl->Sessions.Find(Id) : nullptr;
    return Session && (*Session)->Handlers.Remove(Method) > 0;
}
void FAuroraViewRuntimeModule::OpenDemo()
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

bool FAuroraViewRuntimeModule::RegisterTool(const FString& Method, FAuroraViewHandler Handler, const TSharedPtr<FJsonObject>& Description)
{
    check(IsInGameThread());
    return Impl && !Impl->bStopping && Impl->ControlHost->RegisterTool(Method, MoveTemp(Handler), Description);
}
bool FAuroraViewRuntimeModule::UnregisterTool(const FString& Method)
{
    check(IsInGameThread());
    return Impl && !Impl->bStopping && Impl->ControlHost->UnregisterTool(Method);
}
void FAuroraViewRuntimeModule::CallTool(const FString& Method, const TSharedPtr<FJsonValue>& Params, FAuroraViewCompletion Complete)
{
    check(IsInGameThread());
    if (Impl && !Impl->bStopping) Impl->ControlHost->Call(Method, Params, MoveTemp(Complete));
    else Complete(FAuroraViewReply::Failure(TEXT("HostStoppedError"), TEXT("AuroraView runtime is stopping")));
}
void FAuroraViewRuntimeModule::BroadcastEvent(const FString& Event, const TSharedPtr<FJsonValue>& Detail)
{
    check(IsInGameThread());
    if (!Impl || Impl->bStopping || Event.IsEmpty() || Event.Len() > 256 || Event.StartsWith(TEXT("__auroraview_"))) return;
    TArray<TSharedPtr<FSession>> Snapshot;
    Impl->Sessions.GenerateValueArray(Snapshot);
    for (const auto& Session : Snapshot) Session->EmitPayload(Event, Detail);
    Impl->ControlHost->Broadcast(Event, Detail);
}
IMPLEMENT_MODULE(FAuroraViewRuntimeModule, AuroraViewRuntime)
