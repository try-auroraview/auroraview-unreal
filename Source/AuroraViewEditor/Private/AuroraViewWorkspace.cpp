#include "AuroraViewWorkspace.h"
#include "Framework/Docking/LayoutService.h"
#include "HAL/FileManager.h"
#include "Misc/Paths.h"
#include "Widgets/Docking/SDockTab.h"
#include "Widgets/Input/SButton.h"
#include "Widgets/Layout/SBox.h"
#include "Widgets/SBoxPanel.h"
#include "Widgets/Text/STextBlock.h"

namespace
{
const FName InspectorTab(TEXT("AuroraView.Showcase.Inspector"));
const FName OutlinerTab(TEXT("AuroraView.Showcase.Outliner"));
const FName AssetsTab(TEXT("AuroraView.Showcase.Assets"));
FString LayoutFile()
{
    return FPaths::Combine(FPaths::ProjectSavedDir(), TEXT("Config/AuroraViewLayout.ini"));
}
void Persist(const TSharedRef<FTabManager::FLayout>& Layout)
{
    IFileManager::Get().MakeDirectory(*FPaths::GetPath(LayoutFile()), true);
    FLayoutSaveRestore::SaveToConfig(LayoutFile(), Layout);
}
}

void SAuroraViewWorkspace::Construct(const FArguments& Args)
{
    check(IsInGameThread());
    check(Args._OwnerTab.IsValid() && Args._Inspector.IsValid());
    Inspector = Args._Inspector;
    Outliner = Args._Outliner.IsValid() ? Args._Outliner
        : SNew(STextBlock).Text(FText::FromString(TEXT("Native Outliner: unimplemented in stage 1")));
    Assets = Args._Assets.IsValid() ? Args._Assets
        : SNew(STextBlock).Text(FText::FromString(TEXT("Native asset browser: unimplemented in stage 1")));
    Manager = FGlobalTabmanager::Get()->NewTabManager(Args._OwnerTab.ToSharedRef());
    Manager->SetCanDoDragOperation(true);
    Manager->RegisterTabSpawner(InspectorTab, FOnSpawnTab::CreateSP(this, &SAuroraViewWorkspace::SpawnPanel))
        .SetDisplayName(FText::FromString(TEXT("Core Inspector")));
    Manager->RegisterTabSpawner(OutlinerTab, FOnSpawnTab::CreateSP(this, &SAuroraViewWorkspace::SpawnPanel))
        .SetDisplayName(FText::FromString(TEXT("Native Outliner")));
    Manager->RegisterTabSpawner(AssetsTab, FOnSpawnTab::CreateSP(this, &SAuroraViewWorkspace::SpawnPanel))
        .SetDisplayName(FText::FromString(TEXT("Native Assets")));
    Manager->SetOnPersistLayout(FTabManager::FOnPersistLayout::CreateStatic(&Persist));
    const auto Default = FTabManager::NewLayout(TEXT("AuroraView.NativeShowcase.v1"))
        ->AddArea(FTabManager::NewPrimaryArea()->SetOrientation(Orient_Horizontal)
            ->Split(FTabManager::NewStack()->SetSizeCoefficient(0.23f)->AddTab(OutlinerTab, ETabState::OpenedTab))
            ->Split(FTabManager::NewStack()->SetSizeCoefficient(0.47f)->AddTab(InspectorTab, ETabState::OpenedTab))
            ->Split(FTabManager::NewStack()->SetSizeCoefficient(0.30f)->AddTab(AssetsTab, ETabState::OpenedTab)));
    const auto Layout = FLayoutSaveRestore::LoadFromConfig(LayoutFile(), Default);
    const auto Restored = Manager->RestoreFrom(Layout, TSharedPtr<SWindow>());
    ChildSlot
    [
        SNew(SVerticalBox)
        + SVerticalBox::Slot().AutoHeight().Padding(6)
        [
            SNew(SHorizontalBox)
            + SHorizontalBox::Slot().AutoWidth().Padding(3)
            [ SNew(SButton).Text(FText::FromString(TEXT("Inspector"))).OnClicked_Lambda([this]()
                { Manager->TryInvokeTab(InspectorTab); return FReply::Handled(); }) ]
            + SHorizontalBox::Slot().AutoWidth().Padding(3)
            [ SNew(SButton).Text(FText::FromString(TEXT("Outliner"))).OnClicked_Lambda([this]()
                { Manager->TryInvokeTab(OutlinerTab); return FReply::Handled(); }) ]
            + SHorizontalBox::Slot().AutoWidth().Padding(3)
            [ SNew(SButton).Text(FText::FromString(TEXT("Assets"))).OnClicked_Lambda([this]()
                { Manager->TryInvokeTab(AssetsTab); return FReply::Handled(); }) ]
            + SHorizontalBox::Slot().AutoWidth().Padding(3)
            [ SNew(SButton).Text(FText::FromString(TEXT("Toggle eligible tabs in sidebars")))
                .ToolTipText(FText::FromString(TEXT("Move eligible tabs to temporary native sidebars, or restore remembered tabs")))
                .OnClicked_Lambda([this]()
                { Manager->ToggleSidebarOpenTabs(); return FReply::Handled(); }) ]
            + SHorizontalBox::Slot().AutoWidth().Padding(3)
            [ SNew(SButton).Text(FText::FromString(TEXT("Save layout"))).OnClicked_Lambda([this]()
                { SaveLayout(); return FReply::Handled(); }) ]
        ]
        + SVerticalBox::Slot().FillHeight(1)
        [ Restored.IsValid() ? Restored.ToSharedRef() : StaticCastSharedRef<SWidget>(SNew(STextBlock)
            .Text(FText::FromString(TEXT("Native layout restoration failed")))) ]
    ];
}

TSharedRef<SDockTab> SAuroraViewWorkspace::SpawnPanel(const FSpawnTabArgs& Args)
{
    const FName Name = Args.GetTabId().TabType;
    return SNew(SDockTab).TabRole(ETabRole::PanelTab)
        [ Name == InspectorTab ? Inspector.ToSharedRef() : Name == OutlinerTab ? Outliner.ToSharedRef() : Assets.ToSharedRef() ];
}
void SAuroraViewWorkspace::SaveLayout()
{
    check(IsInGameThread());
    if (Manager) Persist(Manager->PersistLayout());
}
SAuroraViewWorkspace::~SAuroraViewWorkspace()
{
    if (!Manager) return;
    SaveLayout();
    Manager->SetOnPersistLayout(FTabManager::FOnPersistLayout());
    Manager->CloseAllAreas();
    Manager->UnregisterAllTabSpawners();
}
