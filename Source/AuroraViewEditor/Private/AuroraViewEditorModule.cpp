#include "AuroraViewEditorModule.h"
#include "AuroraViewEndpoint.h"
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
    FString Token;
    FString OwnedUrl;
    FString Bridge;
    FString Transport;
    TSharedPtr<IWebBrowserWindow> NativeBrowser;
    TSharedPtr<SWebBrowser> Browser;
    TSharedPtr<SWindow> Window;
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
        FString Type;
        if (!Message->TryGetStringField(TEXT("type"), Type) || Type.IsEmpty())
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
        // Signal Core first, best effort. The page's beforeunload also cancels
        // pending promises; queue invalidation is synchronous and authoritative.
        if (Browser)
        {
            TSharedRef<FJsonObject> Error = MakeShared<FJsonObject>();
            Error->SetStringField(TEXT("message"), TEXT("connection lost: Unreal view closed"));
            Emit(TEXT("backend_error"), Error);
        }
        Mailbox->Close();
        bReady = false;
        bBootAttempted = false;
        SeenIds.Reset();
        if (Browser && Endpoint.IsValid()) Browser->UnbindUObject(TEXT("auroraview"), Endpoint.Get(), true);
        if (Browser) Browser->StopLoad();
        if (Window)
        {
            Window->SetOnWindowClosed(FOnWindowClosed());
            Window->SetContent(SNullWidget::NullWidget);
        }
        Browser.Reset();
        if (NativeBrowser) NativeBrowser->CloseBrowser(true, false);
        NativeBrowser.Reset();
        Endpoint.Reset();
        TSharedPtr<SWindow> OldWindow = MoveTemp(Window);
        if (bDestroyWindow && OldWindow && FSlateApplication::IsInitialized())
            FSlateApplication::Get().RequestDestroyWindow(OldWindow.ToSharedRef());
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
                Mailbox->SetVisible(Window.IsValid() && Window->IsVisible());
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
    bool bStopping = false;
    FString Root;

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
        for (auto& Pair : Sessions)
        {
            Pair.Value->Dispose(true);
            Pair.Value->Mailbox->Stop();
            Pair.Value->Handlers.Reset();
        }
        Sessions.Reset();
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
}

void FAuroraViewEditorModule::ShutdownModule()
{
    check(IsInGameThread());
    if (!Impl) return;
    if (Impl->DemoCommand) IConsoleManager::Get().UnregisterConsoleObject(Impl->DemoCommand, false);
    FCoreDelegates::OnEnginePreExit.Remove(Impl->ExitHandle);
    Impl->Stop();
    Impl.Reset();
}

bool FAuroraViewEditorModule::Open(FName Id, const FString& Fragment, const FText& Title, FString& OutError)
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
    if (Session->Window) return Show(Id);
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
    Session->Browser->BindUObject(TEXT("auroraview"), Session->Endpoint.Get(), true);
    Session->Window = SNew(SWindow).Title(Title).ClientSize(FVector2D(920, 640))
        [ Session->Browser.ToSharedRef() ];
    Session->Browser->SetParentWindow(Session->Window);
    TWeakPtr<FSession> Weak = Session;
    Session->Window->SetOnWindowClosed(FOnWindowClosed::CreateLambda([Weak](const TSharedRef<SWindow>&)
    {
        if (const auto Current = Weak.Pin()) Current->Dispose(false);
    }));
    const auto Parent = FGlobalTabmanager::Get()->GetRootWindow();
    if (Parent) FSlateApplication::Get().AddWindowAsNativeChild(Session->Window.ToSharedRef(), Parent.ToSharedRef());
    else FSlateApplication::Get().AddWindow(Session->Window.ToSharedRef());
    Session->Browser->LoadString(MakeDocument(Stub, Bootstrap, Fragment), Session->OwnedUrl);
    OutError.Empty();
    return true;
}

bool FAuroraViewEditorModule::Show(FName Id)
{
    check(IsInGameThread());
    const auto* Session = Impl ? Impl->Sessions.Find(Id) : nullptr;
    if (!Session || !(*Session)->Window) return false;
    (*Session)->Window->ShowWindow();
    (*Session)->Window->BringToFront();
    (*Session)->Mailbox->SetVisible(true);
    return true;
}
bool FAuroraViewEditorModule::Hide(FName Id)
{
    check(IsInGameThread());
    const auto* Session = Impl ? Impl->Sessions.Find(Id) : nullptr;
    if (!Session || !(*Session)->Window) return false;
    (*Session)->Window->HideWindow();
    (*Session)->Mailbox->SetVisible(false);
    return true;
}
bool FAuroraViewEditorModule::Close(FName Id)
{
    check(IsInGameThread());
    const auto* Session = Impl ? Impl->Sessions.Find(Id) : nullptr;
    if (!Session) return false;
    (*Session)->Dispose(true);
    return true;
}
bool FAuroraViewEditorModule::Remove(FName Id)
{
    check(IsInGameThread());
    if (!Close(Id)) return false;
    Impl->Sessions[Id]->Mailbox->Stop();
    Impl->Sessions.Remove(Id);
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
