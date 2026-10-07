#pragma once

#include "CoreMinimal.h"
#include "Dom/JsonValue.h"
#include "Modules/ModuleInterface.h"

struct AURORAVIEWEDITOR_API FAuroraViewReply
{
    bool bOk = true;
    TSharedPtr<FJsonValue> Result;
    FString ErrorName;
    FString ErrorMessage;
    FString ErrorCode;

    static FAuroraViewReply Success(TSharedPtr<FJsonValue> Value);
    static FAuroraViewReply Failure(FString Name, FString Message, FString Code = TEXT("HOST_ERROR"));
};

// Synchronous, bounded handlers run exclusively on GameThread. Return errors;
// do not throw C++ exceptions, block, or capture raw UObject pointers.
using FAuroraViewHandler = TFunction<FAuroraViewReply(const TSharedPtr<FJsonValue>&)>;

class AURORAVIEWEDITOR_API FAuroraViewEditorModule final : public IModuleInterface
{
public:
    FAuroraViewEditorModule();
    virtual ~FAuroraViewEditorModule() override;
    virtual void StartupModule() override;
    virtual void ShutdownModule() override;
    // UCLASS + CEF require restart instead of unsafe hot DLL unloading.
    virtual bool SupportsDynamicReloading() override { return false; }

    // All public host control APIs require GameThread. IDs survive close/reopen;
    // each Open creates a fresh browser and endpoint, keeping registered handlers.
    bool Open(FName Id, const FString& TrustedHtmlFragment, const FText& Title, FString& OutError);
    bool Show(FName Id);
    bool Hide(FName Id);
    bool Close(FName Id);
    bool Remove(FName Id);
    bool BindCall(FName Id, const FString& Method, FAuroraViewHandler Handler);
    bool UnbindCall(FName Id, const FString& Method);
    void OpenDemo();
private:
    struct FImpl;
    TUniquePtr<FImpl> Impl;
};
