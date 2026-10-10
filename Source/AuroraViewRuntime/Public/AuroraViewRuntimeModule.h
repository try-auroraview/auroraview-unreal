#pragma once

#include "CoreMinimal.h"
#include "Dom/JsonValue.h"
#include "Modules/ModuleInterface.h"

class SDockTab;
class SWidget;
class FJsonObject;
class FTabManager;

// Called on GameThread after the browser is constructed. Native widgets may
// surround it; the browser and RPC session are still owned by this module.
using FAuroraViewDockContent = TFunction<TSharedRef<SWidget>(
    const TSharedRef<SDockTab>&, const TSharedRef<SWidget>&)>;

struct AURORAVIEWRUNTIME_API FAuroraViewReply
{
    bool bOk = true;
    TSharedPtr<FJsonValue> Result;
    FString ErrorName;
    FString ErrorMessage;
    FString ErrorCode;
    TSharedPtr<FJsonValue> ErrorData;

    static FAuroraViewReply Success(TSharedPtr<FJsonValue> Value);
    static FAuroraViewReply Failure(FString Name, FString Message, FString Code = TEXT("HOST_ERROR"), TSharedPtr<FJsonValue> Data = nullptr);
};

// Synchronous, bounded handlers run exclusively on GameThread. Return errors;
// do not throw C++ exceptions, block, or capture raw UObject pointers.
using FAuroraViewHandler = TFunction<FAuroraViewReply(const TSharedPtr<FJsonValue>&)>;
using FAuroraViewCompletion = TFunction<void(FAuroraViewReply)>;

class AURORAVIEWRUNTIME_API FAuroraViewRuntimeModule final : public IModuleInterface
{
public:
    FAuroraViewRuntimeModule();
    virtual ~FAuroraViewRuntimeModule() override;
    virtual void StartupModule() override;
    virtual void ShutdownModule() override;
    // UCLASS + CEF require restart instead of unsafe hot DLL unloading.
    virtual bool SupportsDynamicReloading() override { return false; }

    // All public host control APIs require GameThread. IDs survive close/reopen;
    // each Open creates a fresh browser and endpoint, keeping registered handlers.
    bool Open(FName Id, const FString& TrustedHtmlFragment, const FText& Title, FString& OutError);
    // Register before restoring an Editor layout. Registration does not create CEF.
    // A registered ID cannot be rebound while its view is open.
    bool RegisterDocked(FName Id, const FString& TrustedHtmlFragment, const FText& Title,
        FString& OutError, FAuroraViewDockContent ContentFactory = {});
    bool OpenDocked(FName Id, FString& OutError);
    // Editor control convenience: register a closed view or reuse its current
    // native tab. Changing live content requires an explicit Close first.
    bool OpenDocked(FName Id, const FString& TrustedHtmlFragment, const FText& Title, FString& OutError);
    // Move only this module's live tab into an already-open Editor root stack.
    // The browser/session survives the native move; no tab ownership escapes.
    // Slate assigns a transient document UID while retaining our private type.
    bool DockInTabManager(FName Id, const TSharedRef<FTabManager>& TargetManager,
        FName PlaceholderId, FString& OutError);
#if WITH_DEV_AUTOMATION_TESTS
    // Deterministic native harness seam; never exposed over the Core bridge.
    void FailNextOpenForTesting(FName Id);
#endif
    bool IsReady(FName Id) const;
    // Module-unique live presentation epoch; zero when closed, failed or removed.
    uint64 GetGeneration(FName Id) const;
    // Native Slate state, including whether a dock tab is actually attached to
    // the Editor root window. A dock-capable floating tab is reported honestly.
    TSharedRef<FJsonObject> DescribeView(FName Id) const;
    bool EmitEvent(FName Id, const FString& Event, const TSharedRef<FJsonObject>& Detail);
    bool Show(FName Id);
    bool Hide(FName Id);
    bool Close(FName Id);
    bool Remove(FName Id);
    bool BindCall(FName Id, const FString& Method, FAuroraViewHandler Handler);
    bool UnbindCall(FName Id, const FString& Method);
    void OpenDemo();
    // Shared native/Python tools are nonblocking and always dispatched on GameThread.
    bool RegisterTool(const FString& Method, FAuroraViewHandler Handler, const TSharedPtr<FJsonObject>& Description = nullptr);
    bool UnregisterTool(const FString& Method);
    void CallTool(const FString& Method, const TSharedPtr<FJsonValue>& Params, FAuroraViewCompletion Completion);
    void BroadcastEvent(const FString& Event, const TSharedPtr<FJsonValue>& Detail);
private:
    bool OpenPresentation(FName Id, const FString& TrustedHtmlFragment, const FText& Title,
        FString& OutError, const TSharedPtr<SDockTab>& DockTab);
    struct FImpl;
    TUniquePtr<FImpl> Impl;
};
