#include "AuroraViewNativeDrag.h"
#include "AuroraViewNativeShowcase.h"
#include "DragAndDrop/ActorDragDropOp.h"
#include "DragAndDrop/ActorDragDropGraphEdOp.h"
#include "DragAndDrop/AssetDragDropOp.h"
#include "InputCoreTypes.h"
#include "Widgets/Layout/SBorder.h"
#include "Widgets/SBoxPanel.h"
#include "Widgets/SCompoundWidget.h"
#include "Widgets/Text/STextBlock.h"

namespace
{
class SAuroraNativeSource final : public SCompoundWidget
{
public:
    SLATE_BEGIN_ARGS(SAuroraNativeSource) : _bActors(false) {}
        SLATE_ARGUMENT(TSharedPtr<FAuroraViewNativeShowcase>, Host)
        SLATE_ARGUMENT(bool, bActors)
    SLATE_END_ARGS()
    void Construct(const FArguments& Args)
    {
        Host = Args._Host; bActors = Args._bActors;
        Generation = Args._Host->GetSessionGeneration();
        ChildSlot [ SNew(SBorder).Padding(9)
            [ SNew(STextBlock).Text(FText::FromString(bActors
                ? TEXT("Drag selected actor(s) to native Outliner")
                : TEXT("Drag inspected asset to native viewport"))) ] ];
    }
    FReply OnMouseButtonDown(const FGeometry&, const FPointerEvent& Event) override
    {
        return Event.GetEffectingButton() == EKeys::LeftMouseButton
            ? FReply::Handled().DetectDrag(SharedThis(this), EKeys::LeftMouseButton) : FReply::Unhandled();
    }
    FReply OnDragDetected(const FGeometry&, const FPointerEvent&) override
    {
        check(IsInGameThread());
        const auto Current = Host.Pin();
        if (!Current) return FReply::Unhandled();
        if (bActors)
        {
            FAuroraViewActorDragCapture Capture;
            if (!Current->PrepareActorDrag(Generation, Capture)) return FReply::Unhandled();
            const auto Operation = FActorDragDropGraphEdOp::New(Capture.Actors);
            // Operation construction is another possible native callback boundary.
            if (!Current->FinalizeActorDrag(Capture)) return FReply::Unhandled();
            return FReply::Handled().BeginDragDrop(Operation);
        }
        if (!Current->IsInteractionCurrent(Generation)) return FReply::Unhandled();
        const auto Assets = Current->GetInspectedAssets();
        if (Assets.Num() == 0) return FReply::Unhandled();
        if (Assets.Num() > 32)
        {
            Current->RecordNativeDrag(TEXT("rejected_payload_limit"), TEXT("assets"), Assets.Num());
            return FReply::Unhandled();
        }
        Current->RecordNativeDrag(TEXT("started"), TEXT("assets"), Assets.Num());
        return FReply::Handled().BeginDragDrop(FAssetDragDropOp::New(Assets));
    }
private:
    TWeakPtr<FAuroraViewNativeShowcase> Host;
    uint64 Generation = 0;
    bool bActors = false;
};
class SAuroraNativeTarget final : public SCompoundWidget
{
public:
    SLATE_BEGIN_ARGS(SAuroraNativeTarget) {}
        SLATE_ARGUMENT(TSharedPtr<FAuroraViewNativeShowcase>, Host)
    SLATE_END_ARGS()
    void Construct(const FArguments& Args)
    {
        Host = Args._Host;
        Generation = Args._Host->GetSessionGeneration();
        ChildSlot [ SNew(SBorder).Padding(12)
            [ SNew(STextBlock).Text(FText::FromString(TEXT("Native drop target · drop actors or assets here to inspect"))) ] ];
    }
    FReply OnDragOver(const FGeometry&, const FDragDropEvent& Event) override
    {
        return Event.GetOperationAs<FAssetDragDropOp>().IsValid() || Event.GetOperationAs<FActorDragDropOp>().IsValid()
            ? FReply::Handled() : FReply::Unhandled();
    }
    FReply OnDrop(const FGeometry&, const FDragDropEvent& Event) override
    {
        check(IsInGameThread());
        const auto Current = Host.Pin();
        if (!Current || !Current->IsInteractionCurrent(Generation)) return FReply::Unhandled();
        if (const auto Assets = Event.GetOperationAs<FAssetDragDropOp>())
        {
            if (Assets->GetAssets().Num() == 0 || !Assets->GetAssets()[0].IsValid())
            {
                Current->RecordNativeDrag(TEXT("rejected_unresolved_asset_payload"), TEXT("assets"), 0);
                return FReply::Unhandled();
            }
            Current->InspectAsset(Assets->GetAssets()[0], TEXT("native_drop"));
            Current->RecordNativeDrag(TEXT("accepted_by_inspector"), TEXT("assets"), 1);
            return FReply::Handled();
        }
        if (const auto Actors = Event.GetOperationAs<FActorDragDropOp>())
        {
            const int32 Accepted = Current->InspectActors(Actors->Actors);
            if (Accepted == 0) return FReply::Unhandled();
            Current->RecordNativeDrag(TEXT("accepted_by_inspector"), TEXT("actors"), Accepted);
            return FReply::Handled();
        }
        // Composite/folder/foreign payloads are deliberately unsupported until
        // their exact native child-payload contracts have independent validation.
        Current->RecordNativeDrag(TEXT("rejected_unsupported_payload"), TEXT("unknown"), 0);
        return FReply::Unhandled();
    }
private:
    TWeakPtr<FAuroraViewNativeShowcase> Host;
    uint64 Generation = 0;
};
}
TSharedRef<SWidget> MakeAuroraViewNativeDragSurface(const TSharedRef<FAuroraViewNativeShowcase>& Host)
{
    return SNew(SVerticalBox)
        + SVerticalBox::Slot().AutoHeight().Padding(3) [ SNew(SAuroraNativeSource).Host(Host).bActors(true) ]
        + SVerticalBox::Slot().AutoHeight().Padding(3) [ SNew(SAuroraNativeSource).Host(Host).bActors(false) ]
        + SVerticalBox::Slot().AutoHeight().Padding(3) [ SNew(SAuroraNativeTarget).Host(Host) ];
}
