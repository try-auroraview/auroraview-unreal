#include "AuroraViewNativeShowcase.h"
#include "AuroraViewWorkspace.h"
#include "AuroraViewNativeDrag.h"
#include "ContentBrowserModule.h"
#include "Dom/JsonObject.h"
#include "Editor.h"
#include "Engine/Selection.h"
#include "Engine/World.h"
#include "GameFramework/Actor.h"
#include "HAL/PlatformTime.h"
#include "IContentBrowserSingleton.h"
#include "LevelUtils.h"
#include "Misc/EngineVersion.h"
#include "Modules/ModuleManager.h"
#include "SceneOutlinerModule.h"
#include "ISceneOutliner.h"
#include "SceneOutlinerPublicTypes.h"
#if ENGINE_MAJOR_VERSION == 4
#include "SceneOutlinerFilters.h"
#endif
#include "ScopedTransaction.h"
#include "Components/SceneComponent.h"
#include "Widgets/Text/STextBlock.h"
#include "Widgets/Layout/SBox.h"
#include "Widgets/SNullWidget.h"
#include "Widgets/SBoxPanel.h"

DEFINE_LOG_CATEGORY_STATIC(LogAuroraViewActorDrag, Log, All);

namespace
{
const TCHAR* Methods[] = { TEXT("showcase.snapshot"), TEXT("showcase.subscribe"), TEXT("showcase.unsubscribe"),
    TEXT("showcase.selectActor"), TEXT("showcase.setTransform"), TEXT("showcase.restoreTransform"),
    TEXT("showcase.revealAsset") };
UWorld* EditorWorld()
{
    UWorld* Current = GEditor && !GEditor->PlayWorld ? GEditor->GetEditorWorldContext().World() : nullptr;
    return Current && Current->WorldType == EWorldType::Editor ? Current : nullptr;
}
TSharedPtr<FJsonObject> Object(const TSharedPtr<FJsonValue>& Value)
{
    return Value.IsValid() && Value->Type == EJson::Object ? Value->AsObject() : nullptr;
}
FAuroraViewReply Failure(const TCHAR* Code, const TCHAR* Message)
{
    return FAuroraViewReply::Failure(TEXT("ShowcaseError"), Message, Code);
}
TArray<TSharedPtr<FJsonValue>> VectorJson(const FVector& V)
{
    return { MakeShared<FJsonValueNumber>(V.X), MakeShared<FJsonValueNumber>(V.Y), MakeShared<FJsonValueNumber>(V.Z) };
}
TSharedRef<FJsonObject> TransformJson(const FTransform& T)
{
    const auto O = MakeShared<FJsonObject>();
    O->SetArrayField(TEXT("location"), VectorJson(T.GetLocation()));
    const FRotator R = T.Rotator();
    O->SetArrayField(TEXT("rotation"), VectorJson(FVector(R.Roll, R.Pitch, R.Yaw)));
    O->SetArrayField(TEXT("scale"), VectorJson(T.GetScale3D()));
    return O;
}
bool ReadVector(const TSharedPtr<FJsonObject>& O, const TCHAR* Key, FVector& Out, double Min, double Max)
{
    const TArray<TSharedPtr<FJsonValue>>* A = nullptr;
    if (!O.IsValid() || !O->TryGetArrayField(Key, A) || A->Num() != 3) return false;
    double V[3];
    for (int32 I = 0; I < 3; ++I)
        if (!(*A)[I].IsValid() || (*A)[I]->Type != EJson::Number || !(*A)[I]->TryGetNumber(V[I])
            || !FMath::IsFinite(V[I]) || V[I] < Min || V[I] > Max) return false;
    Out = FVector(V[0], V[1], V[2]);
    return true;
}
bool ReadTransform(const TSharedPtr<FJsonObject>& O, FTransform& Out)
{
    FVector L, R, S;
    if (!ReadVector(O, TEXT("location"), L, -10000000.0, 10000000.0)
        || !ReadVector(O, TEXT("rotation"), R, -360.0, 360.0)
        || !ReadVector(O, TEXT("scale"), S, 0.001, 1000.0)) return false;
    Out = FTransform(FRotator(R.Y, R.Z, R.X), L, S);
    return !Out.ContainsNaN();
}
bool CanEdit(AActor* Actor)
{
    return IsValid(Actor) && !Actor->IsTemplate() && !Actor->IsActorBeingDestroyed()
        && Actor->GetWorld() == EditorWorld() && Actor->GetRootComponent()
#if ENGINE_MAJOR_VERSION >= 5
        && !Actor->IsLockLocation()
#else
        && !Actor->bLockLocation
#endif
        && !FLevelUtils::IsLevelLocked(Actor->GetLevel());
}
}

FName FAuroraViewNativeShowcase::ViewId() { return FName(TEXT("NativeShowcase")); }
uint64 FAuroraViewNativeShowcase::GetSessionGeneration() const { return Module.GetGeneration(Id); }
bool FAuroraViewNativeShowcase::IsInteractionCurrent(uint64 WidgetGeneration) const
{
    return !bStopped && Module.IsReady(Id) && Module.GetGeneration(Id) == WidgetGeneration && EditorWorld();
}
FAuroraViewNativeShowcase::FAuroraViewNativeShowcase(FAuroraViewEditorModule& InModule, FName InViewId)
    : Module(InModule), Id(InViewId) {}
FAuroraViewNativeShowcase::~FAuroraViewNativeShowcase() { Stop(); }
bool FAuroraViewNativeShowcase::Start(const FString& Html, FString& OutError)
{
    check(IsInGameThread());
    if (bStarted || bStopped) { OutError = TEXT("Showcase host already started or stopped"); return false; }
    bStarted = true;
    TWeakPtr<FAuroraViewNativeShowcase> Weak = AsShared();
    for (const TCHAR* Name : Methods)
    {
        const FString Method(Name);
        Module.BindCall(Id, Method, [Weak, Method](const TSharedPtr<FJsonValue>& Params)
        {
            const auto Self = Weak.Pin();
            return Self.IsValid() ? Self->Dispatch(Method, Params) : Failure(TEXT("CLOSED"), TEXT("Showcase host closed"));
        });
    }
    if (!Module.RegisterDocked(Id, Html, FText::FromString(TEXT("AuroraView Native Showcase")), OutError,
        [Weak](const TSharedRef<SDockTab>& Tab, const TSharedRef<SWidget>& Browser) -> TSharedRef<SWidget>
        {
            const auto Self = Weak.Pin();
            return Self.IsValid() ? Self->MakeWorkspace(Tab, Browser) : Browser;
        })) return false;
    AssetSelectionHandle = FModuleManager::LoadModuleChecked<FContentBrowserModule>(TEXT("ContentBrowser"))
        .GetOnAssetSelectionChanged().AddSP(this, &FAuroraViewNativeShowcase::ContentSelectionChanged);
    TickHandle = AuroraViewCompatibility::FTicker::GetCoreTicker().AddTicker(
        FTickerDelegate::CreateSP(this, &FAuroraViewNativeShowcase::Tick), 0.2f);
    return true;
}
void FAuroraViewNativeShowcase::Stop()
{
    check(IsInGameThread());
    if (bStopped) return;
    bStopped = true;
    AuroraViewCompatibility::FTicker::GetCoreTicker().RemoveTicker(TickHandle);
    if (auto* ContentBrowser = FModuleManager::GetModulePtr<FContentBrowserModule>(TEXT("ContentBrowser")))
        ContentBrowser->GetOnAssetSelectionChanged().Remove(AssetSelectionHandle);
    if (bStarted) for (const TCHAR* Name : Methods) Module.UnbindCall(Id, Name);
    bSubscribed = false;
    ClearOutliners();
    OutlinerHosts.Reset(); OutlinerHost.Reset(); OutlinerWorld.Reset();
    ActorsById.Reset(); InspectedAssets.Reset(); LastEditedActor.Reset(); World.Reset();
}
void FAuroraViewNativeShowcase::ClearOutliners()
{
    // Content destruction can run callbacks. Never iterate the mutable owner list.
    const auto Containers = OutlinerHosts;
    OutlinerWorld.Reset();
    for (const auto& WeakContainer : Containers)
    {
        const auto Container = WeakContainer.Pin();
        if (Container.IsValid()) Container->SetContent(SNullWidget::NullWidget);
    }
}
void FAuroraViewNativeShowcase::RefreshScope()
{
    check(IsInGameThread());
    if (bRefreshingScope) return;
    TGuardValue<bool> RefreshGuard(bRefreshingScope, true);
    UWorld* Current = EditorWorld();
    const uint64 CurrentGeneration = Module.GetGeneration(Id);
    if (Scope.IsEmpty() || World.Get() != Current || Generation != CurrentGeneration)
    {
        if (Generation != CurrentGeneration) bSubscribed = false;
        World = Current; Generation = CurrentGeneration; Sequence = 0;
        Scope = FGuid::NewGuid().ToString(EGuidFormats::Digits);
        ActorsById.Reset(); InspectedAssets.Reset(); LastEditedActor.Reset();
        bCanRestore = false; LastNativeDrag.Reset();
        AssetSelectionSource = TEXT("none"); AssetSelectionTime = 0;
        if (AuroraView::CanBuildNativeOutliner(Generation, Module.GetGeneration(Id), bStopped, Current != nullptr))
            RebuildOutliner(Current);
        else ClearOutliners();
    }
    // A retained container must remain empty throughout a closed generation,
    // including later ticks and world changes. Never rebuild for epoch zero.
    if (!Module.GetGeneration(Id) || bStopped) ClearOutliners();
    for (auto It = ActorsById.CreateIterator(); It; ++It)
        if (!It.Value().IsValid() || It.Value()->GetWorld() != Current) It.RemoveCurrent();
}
FString FAuroraViewNativeShowcase::IdFor(AActor* Actor)
{
    for (const auto& Pair : ActorsById) if (Pair.Value.Get() == Actor) return Pair.Key;
    // Prefer current native selection to old history. Evicted IDs fail closed.
    if (ActorsById.Num() >= 256)
    {
        for (auto It = ActorsById.CreateIterator(); It; ++It)
            if (!It.Value().IsValid() || (GEditor && !GEditor->GetSelectedActors()->IsSelected(It.Value().Get())))
            { It.RemoveCurrent(); break; }
        if (ActorsById.Num() >= 256) return FString();
    }
    const FString ActorId = FGuid::NewGuid().ToString(EGuidFormats::Digits);
    ActorsById.Add(ActorId, Actor);
    return ActorId;
}
AActor* FAuroraViewNativeShowcase::ResolveActor(const FString& ActorId)
{
    const auto* Weak = ActorsById.Find(ActorId);
    AActor* Actor = Weak ? Weak->Get() : nullptr;
    return IsValid(Actor) && Actor->GetWorld() == World.Get() ? Actor : nullptr;
}
TArray<TWeakObjectPtr<AActor>> FAuroraViewNativeShowcase::GetSelectedActors() const
{
    TArray<TWeakObjectPtr<AActor>> Result;
    if (!GEditor || !EditorWorld()) return Result;
    for (FSelectionIterator It(*GEditor->GetSelectedActors()); It && Result.Num() < 128; ++It)
    {
        AActor* Actor = Cast<AActor>(*It);
        if (IsValid(Actor) && Actor->GetWorld() == EditorWorld()) Result.Add(Actor);
    }
    return Result;
}
FAuroraViewActorDragCapture FAuroraViewNativeShowcase::CaptureCompleteActorSelection(uint64 WidgetGeneration) const
{
    check(IsInGameThread());
    FAuroraViewActorDragCapture Capture;
    UWorld* IdleWorld = EditorWorld();
    Capture.World = IdleWorld;
    Capture.Admission.World = reinterpret_cast<std::uintptr_t>(IdleWorld);
    Capture.Admission.Generation = Module.GetGeneration(Id);
    Capture.Admission.WidgetGeneration = WidgetGeneration;
    Capture.Admission.Ready = IsInteractionCurrent(WidgetGeneration);
    if (!GEditor || !GEditor->GetSelectedActors()) return Capture;
    // Deliberately inspect every selection entry: no limit, filtering or accepted
    // prefix here. The admission policy rejects the entire set if any entry fails.
    const USelection* Selection = GEditor->GetSelectedActors();
    const int32 SelectedSlots = Selection->Num();
    for (int32 Index = 0; Index < SelectedSlots; ++Index)
    {
        // Indexed access includes null slots; a filtered iterator is insufficient
        // for all-or-reject admission when USelection retains an invalid entry.
        UObject* Object = Selection->GetSelectedObject(Index);
        AActor* Actor = IsValid(Object) ? Cast<AActor>(Object) : nullptr;
        const bool bValidActor = IsValid(Actor);
        AuroraView::ActorDragEntry Entry;
        Entry.Identity = reinterpret_cast<std::uintptr_t>(Object);
        Entry.IsActor = Actor != nullptr;
        Entry.Valid = IsValid(Object);
        Entry.Destroying = bValidActor && Actor->IsActorBeingDestroyed();
        Entry.World = bValidActor ? reinterpret_cast<std::uintptr_t>(Actor->GetWorld()) : 0;
        Capture.Admission.Selection.push_back(Entry);
        Capture.Actors.Add(Actor);
    }
    return Capture;
}
AuroraView::ActorDragDecision FAuroraViewNativeShowcase::CheckActorDrag(const FAuroraViewActorDragCapture& Capture) const
{
    const auto Current = CaptureCompleteActorSelection(Capture.Admission.WidgetGeneration);
    auto Decision = AuroraView::RevalidateActorDrag(Capture.Admission, Current.Admission);
    if (!Decision.Allowed) return Decision;
    // UObject address reuse is insufficient: every original weak identity and
    // world must still be valid, even if the fresh native selection has that address.
    if (!Capture.World.IsValid() || Capture.World.Get() != EditorWorld()
        || Capture.Actors.Num() != static_cast<int32>(Capture.Admission.Selection.size()))
    { Decision.Allowed = false; Decision.Reason = "captured_world_or_payload_changed"; return Decision; }
    for (const auto& Actor : Capture.Actors)
    {
        if (!Actor.IsValid() || Actor->IsActorBeingDestroyed() || Actor->GetWorld() != Capture.World.Get())
        { Decision.Allowed = false; Decision.Reason = "captured_actor_invalidated";
            ++Decision.Invalid; if (Decision.Eligible) --Decision.Eligible; return Decision; }
    }
    return Decision;
}
void FAuroraViewNativeShowcase::RecordActorDragDecision(const TCHAR* Phase, const AuroraView::ActorDragDecision& Decision)
{
    // Intentionally no RefreshScope, widget teardown or public callbacks here.
    // Final admission must have no such boundary before BeginDragDrop.
    LastNativeDrag = MakeShared<FJsonObject>();
    LastNativeDrag->SetStringField(TEXT("phase"), Phase);
    LastNativeDrag->SetStringField(TEXT("kind"), TEXT("actors"));
    LastNativeDrag->SetStringField(TEXT("reason"), UTF8_TO_TCHAR(Decision.Reason));
    LastNativeDrag->SetNumberField(TEXT("count"), Decision.Allowed ? Decision.Selected : 0);
    LastNativeDrag->SetNumberField(TEXT("selectedCount"), Decision.Selected);
    LastNativeDrag->SetNumberField(TEXT("eligibleCount"), Decision.Eligible);
    LastNativeDrag->SetNumberField(TEXT("invalidCount"), Decision.Invalid);
    LastNativeDrag->SetNumberField(TEXT("foreignWorldCount"), Decision.ForeignWorld);
    LastNativeDrag->SetNumberField(TEXT("destroyingCount"), Decision.Destroying);
    LastNativeDrag->SetNumberField(TEXT("nonActorCount"), Decision.NonActors);
    LastNativeDrag->SetNumberField(TEXT("limit"), AuroraView::MaxActorDragSelection);
    LastNativeDrag->SetNumberField(TEXT("hostSeconds"), FPlatformTime::Seconds());
    LastNativeDrag->SetStringField(TEXT("generation"), AuroraViewCompatibility::UInt64String(Module.GetGeneration(Id)));
    if (!Decision.Allowed)
        UE_LOG(LogAuroraViewActorDrag, Warning, TEXT("Actor drag rejected: %s; selected=%d eligible=%d limit=%d"),
            UTF8_TO_TCHAR(Decision.Reason), static_cast<int32>(Decision.Selected),
            static_cast<int32>(Decision.Eligible), static_cast<int32>(AuroraView::MaxActorDragSelection));
}
bool FAuroraViewNativeShowcase::PrepareActorDrag(uint64 WidgetGeneration, FAuroraViewActorDragCapture& OutCapture)
{
    OutCapture = CaptureCompleteActorSelection(WidgetGeneration);
    auto Decision = AuroraView::AdmitActorDrag(OutCapture.Admission);
    if (!Decision.Allowed) { RecordActorDragDecision(TEXT("rejected"), Decision); return false; }
    // Feedback refresh may rebuild/retire native children and run callbacks.
    RecordNativeDrag(TEXT("preparing"), TEXT("actors"), static_cast<int32>(Decision.Selected));
#if WITH_DEV_AUTOMATION_TESTS
    const auto Callback = MoveTemp(ActorDragFeedbackForTesting);
    if (Callback) Callback();
#endif
    Decision = CheckActorDrag(OutCapture);
    if (!Decision.Allowed) RecordActorDragDecision(TEXT("rejected"), Decision);
    return Decision.Allowed;
}
bool FAuroraViewNativeShowcase::FinalizeActorDrag(const FAuroraViewActorDragCapture& Capture)
{
    // Invoked after native operation construction, immediately before BeginDragDrop.
    const auto Decision = CheckActorDrag(Capture);
    RecordActorDragDecision(Decision.Allowed ? TEXT("started") : TEXT("rejected"), Decision);
    return Decision.Allowed;
}
TArray<FAssetData> FAuroraViewNativeShowcase::GetInspectedAssets() const { return InspectedAssets; }
TSharedRef<FJsonObject> FAuroraViewNativeShowcase::Snapshot()
{
    check(IsInGameThread());
    RefreshScope();
    const auto State = MakeShared<FJsonObject>();
    State->SetStringField(TEXT("engineVersion"), FEngineVersion::Current().ToString());
    State->SetStringField(TEXT("generation"), AuroraViewCompatibility::UInt64String(Generation));
    State->SetStringField(TEXT("scope"), Scope);
    State->SetStringField(TEXT("sequence"), AuroraViewCompatibility::UInt64String(++Sequence));
    State->SetNumberField(TEXT("hostSeconds"), FPlatformTime::Seconds());
    State->SetBoolField(TEXT("gameThread"), IsInGameThread());
    State->SetBoolField(TEXT("editorWorldAvailable"), World.IsValid());
    State->SetStringField(TEXT("crossWindowInputStatus"), TEXT("unverified_engine_parent_cache_risk"));
    State->SetBoolField(TEXT("canRestore"), bCanRestore && LastEditedActor.IsValid());
    const auto Selected = GetSelectedActors();
    TArray<TSharedPtr<FJsonValue>> Selection;
    for (const auto& Weak : Selected)
    {
        const FString ActorId = IdFor(Weak.Get());
        if (!ActorId.IsEmpty()) Selection.Add(MakeShared<FJsonValueString>(ActorId));
    }
    int32 ActualSelectedCount = 0;
    if (GEditor) for (FSelectionIterator It(*GEditor->GetSelectedActors()); It; ++It)
    {
        const AActor* Actor = Cast<AActor>(*It);
        if (IsValid(Actor) && Actor->GetWorld() == World.Get()) ++ActualSelectedCount;
    }
    State->SetNumberField(TEXT("selectedActorCount"), ActualSelectedCount);
    State->SetBoolField(TEXT("selectionTruncated"), ActualSelectedCount != Selection.Num());
    State->SetArrayField(TEXT("selectedActorIds"), Selection);
    TArray<TSharedPtr<FJsonValue>> Actors;
    for (const auto& Pair : ActorsById)
    {
        AActor* Actor = Pair.Value.Get();
        if (!IsValid(Actor)) continue;
        const auto Item = MakeShared<FJsonObject>();
        Item->SetStringField(TEXT("id"), Pair.Key);
        Item->SetStringField(TEXT("label"), Actor->GetActorLabel());
        Item->SetStringField(TEXT("class"), Actor->GetClass()->GetName());
        Item->SetBoolField(TEXT("selected"), GEditor && GEditor->GetSelectedActors()->IsSelected(Actor));
        Item->SetBoolField(TEXT("editable"), CanEdit(Actor));
        Item->SetObjectField(TEXT("transform"), TransformJson(Actor->GetActorTransform()));
        Actors.Add(MakeShared<FJsonValueObject>(Item));
    }
    State->SetArrayField(TEXT("actors"), Actors);
    TArray<TSharedPtr<FJsonValue>> Assets;
    for (const auto& Asset : InspectedAssets)
    {
        const auto Item = MakeShared<FJsonObject>();
        Item->SetStringField(TEXT("name"), Asset.AssetName.ToString());
#if ENGINE_MAJOR_VERSION >= 5
        Item->SetStringField(TEXT("class"), Asset.AssetClassPath.ToString());
#else
        Item->SetStringField(TEXT("class"), Asset.AssetClass.ToString());
#endif
        Assets.Add(MakeShared<FJsonValueObject>(Item));
    }
    State->SetArrayField(TEXT("assets"), Assets);
    if (LastNativeDrag.IsValid()) State->SetObjectField(TEXT("nativeDrag"), LastNativeDrag.ToSharedRef());
    State->SetStringField(TEXT("assetSelectionSource"), AssetSelectionSource);
    State->SetNumberField(TEXT("assetSelectionHostSeconds"), AssetSelectionTime);
    return State;
}
bool FAuroraViewNativeShowcase::Tick(float)
{
    if (bStopped) return false;
    RefreshScope();
    if (!Module.IsReady(Id)) { bSubscribed = false; return true; }
    if (bSubscribed) Module.EmitEvent(Id, TEXT("showcase.state"), Snapshot());
    return true;
}
void FAuroraViewNativeShowcase::ContentSelectionChanged(const TArray<FAssetData>& Assets, bool bIsPrimaryBrowser)
{
    check(IsInGameThread());
    if (bStopped || !bIsPrimaryBrowser || !Module.IsReady(Id)) return;
    RefreshScope();
    InspectedAssets.Reset();
    for (const auto& Asset : Assets)
        if (Asset.IsValid() && InspectedAssets.Num() < 128) InspectedAssets.Add(Asset);
    AssetSelectionSource = TEXT("primary_content_browser_event");
    AssetSelectionTime = FPlatformTime::Seconds();
}
void FAuroraViewNativeShowcase::InspectAsset(const FAssetData& Asset, const FString& Source)
{
    check(IsInGameThread()); RefreshScope();
    InspectedAssets.Reset();
    if (Asset.IsValid()) InspectedAssets.Add(Asset);
    AssetSelectionSource = Source; AssetSelectionTime = FPlatformTime::Seconds();
}
int32 FAuroraViewNativeShowcase::InspectActors(const TArray<TWeakObjectPtr<AActor>>& Actors)
{
    check(IsInGameThread()); RefreshScope();
    int32 Accepted = 0;
    for (const auto& Actor : Actors)
        if (Actor.IsValid() && Actor->GetWorld() == World.Get() && !IdFor(Actor.Get()).IsEmpty()) ++Accepted;
    return Accepted;
}
void FAuroraViewNativeShowcase::RecordNativeDrag(const FString& Phase, const FString& Kind, int32 Count)
{
    check(IsInGameThread()); RefreshScope();
    LastNativeDrag = MakeShared<FJsonObject>();
    LastNativeDrag->SetStringField(TEXT("phase"), Phase);
    LastNativeDrag->SetStringField(TEXT("kind"), Kind);
    LastNativeDrag->SetNumberField(TEXT("count"), Count);
    LastNativeDrag->SetNumberField(TEXT("hostSeconds"), FPlatformTime::Seconds());
    LastNativeDrag->SetStringField(TEXT("generation"), AuroraViewCompatibility::UInt64String(Generation));
    // Started means only that Slate created an operation. Destination acceptance
    // and viewport/Outliner host changes must be observed independently.
}
TSharedRef<SWidget> FAuroraViewNativeShowcase::MakeWorkspace(const TSharedRef<SDockTab>& Tab,
    const TSharedRef<SWidget>& Browser)
{
    FAssetPickerConfig Config;
    Config.InitialAssetViewType = EAssetViewType::Tile;
    Config.SelectionMode = ESelectionMode::Single;
    Config.bAllowDragging = true;
    const uint64 WidgetGeneration = GetSessionGeneration();
    TWeakPtr<FAuroraViewNativeShowcase> PickerHost = AsShared();
    Config.OnAssetSelected = FOnAssetSelected::CreateLambda([PickerHost, WidgetGeneration](const FAssetData& Asset)
    {
        const auto Self = PickerHost.Pin();
        if (Self.IsValid() && Self->IsInteractionCurrent(WidgetGeneration)) Self->InspectAsset(Asset);
    });
    const auto AssetWidget = FModuleManager::LoadModuleChecked<FContentBrowserModule>(TEXT("ContentBrowser"))
        .Get().CreateAssetPicker(Config);
    RefreshScope();
    TWeakPtr<FAuroraViewNativeShowcase> Weak = AsShared();
    // A private panel retained by Slate must not retain an active old Outliner.
    for (const auto& Old : OutlinerHosts)
    {
        const auto Container = Old.Pin();
        if (Container.IsValid()) Container->SetContent(SNullWidget::NullWidget);
    }
    OutlinerHosts.RemoveAll([](const TWeakPtr<SBox>& Old) { return !Old.IsValid(); });
    const auto OutlinerContainer = SNew(SBox).IsEnabled_Lambda([Weak, WidgetGeneration]()
    {
        const auto Self = Weak.Pin();
        return Self.IsValid() && Self->IsInteractionCurrent(WidgetGeneration)
            && Self->OutlinerWorld.IsValid() && Self->OutlinerWorld.Get() == EditorWorld();
    });
    OutlinerHost = OutlinerContainer;
    OutlinerHosts.Add(OutlinerContainer);
    RebuildOutliner(EditorWorld());
    const auto Inspector = SNew(SVerticalBox)
        + SVerticalBox::Slot().AutoHeight() [ MakeAuroraViewNativeDragSurface(AsShared()) ]
        + SVerticalBox::Slot().FillHeight(1) [ Browser ];
    return SNew(SAuroraViewWorkspace).OwnerTab(Tab).Inspector(Inspector).Assets(AssetWidget).Outliner(OutlinerContainer);
}
void FAuroraViewNativeShowcase::RebuildOutliner(UWorld* Current)
{
    const uint64 BuildingGeneration = Generation;
    if (!AuroraView::CanBuildNativeOutliner(BuildingGeneration, Module.GetGeneration(Id), bStopped,
        Current && Current == EditorWorld()))
    { ClearOutliners(); return; }
    const auto Container = OutlinerHost.Pin();
    if (!Container.IsValid()) return;
    Container->SetContent(SNullWidget::NullWidget);
    if (!AuroraView::CanBuildNativeOutliner(BuildingGeneration, Module.GetGeneration(Id), bStopped,
        Current == EditorWorld()))
    { ClearOutliners(); return; }
#if ENGINE_MAJOR_VERSION >= 5
    FSceneOutlinerInitializationOptions Options;
    Options.bShowHeaderRow = true;
    const auto Outliner = FModuleManager::LoadModuleChecked<FSceneOutlinerModule>(TEXT("SceneOutliner"))
        .CreateActorBrowser(Options, TWeakObjectPtr<UWorld>(Current));
#else
    SceneOutliner::FInitializationOptions Options;
    Options.bShowHeaderRow = true;
    Options.Mode = ESceneOutlinerMode::ActorBrowsing;
#if ENGINE_MINOR_VERSION >= 26
    Options.SpecifiedWorldToDisplay = Current;
#endif
    // UE4.18 lacks the world argument. Its native browser follows Editor state;
    // filter foreign-world actors and retain the same generation/world guards.
    const TWeakObjectPtr<UWorld> BrowsingWorld(Current);
    Options.Filters->AddFilterPredicate(SceneOutliner::FActorFilterPredicate::CreateLambda(
        [BrowsingWorld](const AActor* Actor)
        { return BrowsingWorld.IsValid() && IsValid(Actor) && Actor->GetWorld() == BrowsingWorld.Get(); }),
        SceneOutliner::EDefaultFilterBehaviour::Pass);
    const auto Outliner = FModuleManager::LoadModuleChecked<FSceneOutlinerModule>(TEXT("SceneOutliner"))
        .CreateSceneOutliner(Options, FOnActorPicked());
#endif
#if WITH_DEV_AUTOMATION_TESTS
    ++OutlinerBuildCountForTesting;
#endif
    if (!AuroraView::CanBuildNativeOutliner(BuildingGeneration, Module.GetGeneration(Id), bStopped,
        Current == EditorWorld()) || OutlinerHost.Pin() != Container)
    { ClearOutliners(); return; }
    OutlinerWorld = Current;
    Container->SetContent(Outliner);
    if (!AuroraView::CanBuildNativeOutliner(BuildingGeneration, Module.GetGeneration(Id), bStopped,
        Current == EditorWorld())) ClearOutliners();
}
FAuroraViewReply FAuroraViewNativeShowcase::SelectActor(const TSharedPtr<FJsonObject>& Params)
{
    FString ActorId;
    if (!Params.IsValid() || !Params->TryGetStringField(TEXT("actorId"), ActorId)) return Failure(TEXT("INVALID_PARAMS"), TEXT("actorId required"));
    AActor* Actor = ResolveActor(ActorId);
    if (!Actor || !GEditor) return Failure(TEXT("STALE_ACTOR"), TEXT("Actor is absent from this world/session"));
    const TWeakObjectPtr<AActor> WeakActor(Actor);
    const TWeakObjectPtr<UWorld> StartedWorld(World);
    const uint64 StartedGeneration = Module.GetGeneration(this->Id);
    const auto Current = [this, WeakActor, StartedWorld, StartedGeneration]()
    { return !bStopped && WeakActor.IsValid() && StartedWorld.IsValid() && EditorWorld() == StartedWorld.Get()
        && WeakActor->GetWorld() == StartedWorld.Get() && Module.GetGeneration(this->Id) == StartedGeneration; };
    GEditor->SelectNone(false, true, false);
    if (!Current()) return Failure(TEXT("INTERRUPTED"), TEXT("Selection target or session changed during native callback"));
    GEditor->SelectActor(WeakActor.Get(), true, true);
    if (!Current()) return Failure(TEXT("INTERRUPTED"), TEXT("Selection target or session changed during native callback"));
    const auto Result = Snapshot();
    Result->SetBoolField(TEXT("selectionMatched"), Current() && GEditor->GetSelectedActors()->IsSelected(WeakActor.Get())
        && GetSelectedActors().Num() == 1);
    return FAuroraViewReply::Success(MakeShared<FJsonValueObject>(Result));
}
FAuroraViewReply FAuroraViewNativeShowcase::SetTransform(const TSharedPtr<FJsonObject>& Params)
{
    FString ActorId;
    const TSharedPtr<FJsonObject>* Desired = nullptr;
    const TSharedPtr<FJsonObject>* Expected = nullptr;
    FTransform Next, Previous;
    if (!Params.IsValid() || !Params->TryGetStringField(TEXT("actorId"), ActorId)
        || !Params->TryGetObjectField(TEXT("transform"), Desired) || !ReadTransform(*Desired, Next)
        || !Params->TryGetObjectField(TEXT("expected"), Expected) || !ReadTransform(*Expected, Previous))
        return Failure(TEXT("INVALID_PARAMS"), TEXT("Finite bounded location/rotation/positive scale and expected transform required"));
    AActor* Actor = ResolveActor(ActorId);
    if (!CanEdit(Actor) || !GEditor->GetSelectedActors()->IsSelected(Actor) || GetSelectedActors().Num() != 1)
        return Failure(TEXT("NOT_EDITABLE"), TEXT("Select one valid unlocked Editor actor with a root component"));
    if (!Actor->GetActorTransform().Equals(Previous, 0.0001))
        return Failure(TEXT("CONFLICT"), TEXT("Actor changed since this form was read; refresh before editing"));
    const FTransform Original = Actor->GetActorTransform();
    const TWeakObjectPtr<AActor> WeakActor(Actor);
    const TWeakObjectPtr<USceneComponent> WeakRoot(Actor->GetRootComponent());
    const TWeakObjectPtr<UWorld> StartedWorld(World);
    const uint64 StartedGeneration = Module.GetGeneration(this->Id);
    const FString StartedScope = Scope;
    const auto Current = [this, WeakActor, WeakRoot, StartedWorld, StartedGeneration, StartedScope]()
    { return !bStopped && Scope == StartedScope && Module.GetGeneration(this->Id) == StartedGeneration
        && StartedWorld.IsValid() && EditorWorld() == StartedWorld.Get() && CanEdit(WeakActor.Get())
        && WeakRoot.IsValid() && WeakActor->GetRootComponent() == WeakRoot.Get(); };
    const auto Interrupted = [this]()
    { bCanRestore = false; LastEditedActor.Reset(); return Failure(TEXT("INTERRUPTED"),
        TEXT("Native callback changed actor/root/world/session; inspect actual host state before another edit")); };
    FScopedTransaction Transaction(FText::FromString(TEXT("AuroraView validated transform")));
    WeakActor->Modify();
    if (!Current()) return Interrupted();
    WeakRoot->Modify();
    if (!Current()) return Interrupted();
    const bool bSet = WeakActor->SetActorTransform(Next, false, nullptr, ETeleportType::TeleportPhysics);
    if (!Current()) return Interrupted();
    if (!bSet)
    {
        const bool bRestored = WeakActor->SetActorTransform(Original, false, nullptr, ETeleportType::TeleportPhysics);
        if (!Current()) return Interrupted();
        if (bRestored && WeakActor->GetActorTransform().Equals(Original, 0.0001)) Transaction.Cancel();
        return Failure(TEXT("HOST_REJECTED"), TEXT("Host rejected the transform; inspect actual state and native Undo before retrying"));
    }
    WeakActor->PostEditMove(true);
    if (!Current()) return Interrupted();
    GEditor->RedrawLevelEditingViewports();
    if (!Current()) return Interrupted();
    LastEditedActor = WeakActor; BeforeEdit = Original; AfterEdit = WeakActor->GetActorTransform(); bCanRestore = true;
    const bool bMatched = AfterEdit.Equals(Next, 0.0001);
    const auto Result = Snapshot();
    if (!Current()) return Interrupted();
    Result->SetBoolField(TEXT("transformMatched"), bMatched);
    return FAuroraViewReply::Success(MakeShared<FJsonValueObject>(Result));
}
FAuroraViewReply FAuroraViewNativeShowcase::RestoreTransform()
{
    AActor* Actor = LastEditedActor.Get();
    if (!bCanRestore || !CanEdit(Actor)) return Failure(TEXT("NO_EDIT"), TEXT("No restorable transform in this world/session"));
    if (!Actor->GetActorTransform().Equals(AfterEdit, 0.0001))
        return Failure(TEXT("CONFLICT"), TEXT("Actor changed after the edit; restore refused to overwrite that change"));
    const TWeakObjectPtr<AActor> WeakActor(Actor);
    const TWeakObjectPtr<USceneComponent> WeakRoot(Actor->GetRootComponent());
    const TWeakObjectPtr<UWorld> StartedWorld(World);
    const uint64 StartedGeneration = Module.GetGeneration(Id);
    const FString StartedScope = Scope;
    const FTransform Original = Actor->GetActorTransform(), Target = BeforeEdit;
    const auto Current = [this, WeakActor, WeakRoot, StartedWorld, StartedGeneration, StartedScope]()
    { return !bStopped && Scope == StartedScope && Module.GetGeneration(Id) == StartedGeneration
        && StartedWorld.IsValid() && EditorWorld() == StartedWorld.Get() && CanEdit(WeakActor.Get())
        && WeakRoot.IsValid() && WeakActor->GetRootComponent() == WeakRoot.Get(); };
    const auto Interrupted = [this]()
    { bCanRestore = false; LastEditedActor.Reset(); return Failure(TEXT("INTERRUPTED"),
        TEXT("Native callback changed restore target/root/world/session; inspect actual state and native Undo")); };
    FScopedTransaction Transaction(FText::FromString(TEXT("AuroraView restore transform")));
    WeakActor->Modify();
    if (!Current()) return Interrupted();
    WeakRoot->Modify();
    if (!Current()) return Interrupted();
    const bool bSet = WeakActor->SetActorTransform(Target, false, nullptr, ETeleportType::TeleportPhysics);
    if (!Current()) return Interrupted();
    if (!bSet)
    {
        const bool bRestored = WeakActor->SetActorTransform(Original, false, nullptr, ETeleportType::TeleportPhysics);
        if (!Current()) return Interrupted();
        if (bRestored && WeakActor->GetActorTransform().Equals(Original, 0.0001)) Transaction.Cancel();
        return Failure(TEXT("HOST_REJECTED"), TEXT("Host rejected restore; inspect actual state and native Undo before retrying"));
    }
    WeakActor->PostEditMove(true);
    if (!Current()) return Interrupted();
    GEditor->RedrawLevelEditingViewports();
    if (!Current()) return Interrupted();
    bCanRestore = false;
    return FAuroraViewReply::Success(MakeShared<FJsonValueObject>(Snapshot()));
}

FAuroraViewReply FAuroraViewNativeShowcase::Dispatch(const FString& Method, const TSharedPtr<FJsonValue>& Params)
{
    check(IsInGameThread());
    RefreshScope();
    if (bStopped) return Failure(TEXT("CLOSED"), TEXT("Showcase host closed"));
    if (Method == TEXT("showcase.setTransform") || Method == TEXT("showcase.restoreTransform") || Method == TEXT("showcase.selectActor"))
    {
        if (bMutating) return Failure(TEXT("BUSY"), TEXT("A native mutation callback is already active"));
        TGuardValue<bool> MutationGuard(bMutating, true);
        if (Method == TEXT("showcase.setTransform")) return SetTransform(Object(Params));
        if (Method == TEXT("showcase.restoreTransform")) return RestoreTransform();
        return SelectActor(Object(Params));
    }
    if (Method == TEXT("showcase.subscribe"))
    {
        if (!bSubscribed)
        {
            TArray<FAssetData> InitialAssets;
            FModuleManager::LoadModuleChecked<FContentBrowserModule>(TEXT("ContentBrowser")).Get()
                .GetSelectedAssets(InitialAssets);
            ContentSelectionChanged(InitialAssets, true);
            AssetSelectionSource = TEXT("primary_content_browser_snapshot");
        }
        bSubscribed = true;
    }
    else if (Method == TEXT("showcase.unsubscribe")) bSubscribed = false;
    else if (Method == TEXT("showcase.revealAsset"))
    {
        if (InspectedAssets.Num() == 0) return Failure(TEXT("NO_ASSET"), TEXT("Choose a real asset in the native picker first"));
        FModuleManager::LoadModuleChecked<FContentBrowserModule>(TEXT("ContentBrowser")).Get()
            .SyncBrowserToAssets(InspectedAssets);
        const auto Result = MakeShared<FJsonObject>();
        Result->SetStringField(TEXT("status"), TEXT("submitted"));
        Result->SetNumberField(TEXT("submittedHostSeconds"), FPlatformTime::Seconds());
        Result->SetStringField(TEXT("note"), TEXT("Content Browser processes sync on a later tick and may open a browser window"));
        return FAuroraViewReply::Success(MakeShared<FJsonValueObject>(Result));
    }
    else if (Method != TEXT("showcase.snapshot")) return Failure(TEXT("METHOD_NOT_FOUND"), TEXT("Unknown typed showcase method"));
    return FAuroraViewReply::Success(MakeShared<FJsonValueObject>(Snapshot()));
}
