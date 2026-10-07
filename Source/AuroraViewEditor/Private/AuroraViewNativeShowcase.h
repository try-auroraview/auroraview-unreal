#pragma once

#include "CoreMinimal.h"
#include "AuroraViewEditorModule.h"
#include "AssetRegistry/AssetData.h"
#include "Containers/Ticker.h"
#include "NativeInteractionGuards.h"

class AActor;
class UWorld;
class SBox;

struct FAuroraViewActorDragCapture
{
    AuroraView::ActorDragSnapshot Admission;
    TWeakObjectPtr<UWorld> World;
    TArray<TWeakObjectPtr<AActor>> Actors;
};

// Demo/acceptance host for one view. Opaque IDs are weak, world- and generation-
// scoped. There is no object-path loading, eval endpoint or generic property API.
class FAuroraViewNativeShowcase final : public TSharedFromThis<FAuroraViewNativeShowcase>
{
public:
    explicit FAuroraViewNativeShowcase(FAuroraViewEditorModule& InModule, FName InViewId = ViewId());
    ~FAuroraViewNativeShowcase();
    bool Start(const FString& Html, FString& OutError);
    void Stop();
    TSharedRef<FJsonObject> Snapshot();
    FAuroraViewReply Dispatch(const FString& Method, const TSharedPtr<FJsonValue>& Params);
    void InspectAsset(const FAssetData& Asset, const FString& Source = TEXT("asset_picker"));
    int32 InspectActors(const TArray<TWeakObjectPtr<AActor>>& Actors);
    void RecordNativeDrag(const FString& Phase, const FString& Kind, int32 Count);
    // Inspector sample only. Native outbound drag has separate all-or-reject admission.
    TArray<TWeakObjectPtr<AActor>> GetSelectedActors() const;
    bool PrepareActorDrag(uint64 WidgetGeneration, FAuroraViewActorDragCapture& OutCapture);
    bool FinalizeActorDrag(const FAuroraViewActorDragCapture& Capture);
#if WITH_DEV_AUTOMATION_TESTS
    void SetActorDragFeedbackForTesting(TFunction<void()> Callback) { ActorDragFeedbackForTesting = MoveTemp(Callback); }
    TSharedPtr<SBox> GetOutlinerForTesting() const { return OutlinerHost.Pin(); }
    uint64 GetOutlinerBuildCountForTesting() const { return OutlinerBuildCountForTesting; }
#endif
    TArray<FAssetData> GetInspectedAssets() const;
    static FName ViewId();
    uint64 GetSessionGeneration() const;
    bool IsInteractionCurrent(uint64 WidgetGeneration) const;
private:
    TSharedRef<SWidget> MakeWorkspace(const TSharedRef<SDockTab>& Tab, const TSharedRef<SWidget>& Browser);
    bool Tick(float Delta);
    void RefreshScope();
    void ClearOutliners();
    FAuroraViewActorDragCapture CaptureCompleteActorSelection(uint64 WidgetGeneration) const;
    AuroraView::ActorDragDecision CheckActorDrag(const FAuroraViewActorDragCapture& Capture) const;
    void RecordActorDragDecision(const TCHAR* Phase, const AuroraView::ActorDragDecision& Decision);
    void RebuildOutliner(UWorld* Current);
    void ContentSelectionChanged(const TArray<FAssetData>& Assets, bool bIsPrimaryBrowser);
    FString IdFor(AActor* Actor);
    AActor* ResolveActor(const FString& Id);
    FAuroraViewReply SetTransform(const TSharedPtr<FJsonObject>& Params);
    FAuroraViewReply RestoreTransform();
    FAuroraViewReply SelectActor(const TSharedPtr<FJsonObject>& Params);
    FAuroraViewEditorModule& Module;
    FName Id;
    FTSTicker::FDelegateHandle TickHandle;
    FDelegateHandle AssetSelectionHandle;
    TWeakObjectPtr<UWorld> World;
    TWeakObjectPtr<UWorld> OutlinerWorld;
    TWeakPtr<SBox> OutlinerHost;
    TArray<TWeakPtr<SBox>> OutlinerHosts;
    TMap<FString, TWeakObjectPtr<AActor>> ActorsById;
    TArray<FAssetData> InspectedAssets;
    FString AssetSelectionSource = TEXT("none");
    double AssetSelectionTime = 0;
    TWeakObjectPtr<AActor> LastEditedActor;
    FTransform BeforeEdit, AfterEdit;
    FString Scope;
    TSharedPtr<FJsonObject> LastNativeDrag;
    uint64 Generation = 0, Sequence = 0;
#if WITH_DEV_AUTOMATION_TESTS
    TFunction<void()> ActorDragFeedbackForTesting;
    uint64 OutlinerBuildCountForTesting = 0;
#endif
    bool bRefreshingScope = false;
    bool bMutating = false;
    bool bSubscribed = false, bStopped = false, bCanRestore = false, bStarted = false;
};
