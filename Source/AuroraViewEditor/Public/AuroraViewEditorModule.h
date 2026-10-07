#pragma once

#include "CoreMinimal.h"
#include "Dom/JsonValue.h"
#include "Modules/ModuleInterface.h"

class SDockTab;
class SWidget;
class FJsonObject;

#include "AuroraViewRuntimeModule.h"

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
    // Register before restoring an Editor layout. Registration does not create CEF.
    // A registered ID cannot be rebound while its view is open.
    bool RegisterDocked(FName Id, const FString& TrustedHtmlFragment, const FText& Title,
        FString& OutError, FAuroraViewDockContent ContentFactory = {});
    bool OpenDocked(FName Id, FString& OutError);
#if WITH_DEV_AUTOMATION_TESTS
    // Deterministic native harness seam; never exposed over the Core bridge.
    void FailNextOpenForTesting(FName Id);
#endif
    bool IsReady(FName Id) const;
    // Module-unique live presentation epoch; zero when closed, failed or removed.
    uint64 GetGeneration(FName Id) const;
    bool EmitEvent(FName Id, const FString& Event, const TSharedRef<FJsonObject>& Detail);
    bool Show(FName Id);
    bool Hide(FName Id);
    bool Close(FName Id);
    bool Remove(FName Id);
    bool BindCall(FName Id, const FString& Method, FAuroraViewHandler Handler);
    bool UnbindCall(FName Id, const FString& Method);
    void OpenDemo();
private:
    void InitializeShowcase();
    struct FImpl;
    TUniquePtr<FImpl> Impl;
};
