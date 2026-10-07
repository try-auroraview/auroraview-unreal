#pragma once

#include "CoreMinimal.h"
#include "Widgets/SCompoundWidget.h"
#include "Framework/Docking/TabManager.h"

class SDockTab;

// Each outer SDockTab owns its manager and registered stable child tab types.
// It never replaces a global/LevelEditor layout persistence callback.
class SAuroraViewWorkspace final : public SCompoundWidget
{
public:
    SLATE_BEGIN_ARGS(SAuroraViewWorkspace) {}
        SLATE_ARGUMENT(TSharedPtr<SDockTab>, OwnerTab)
        SLATE_ARGUMENT(TSharedPtr<SWidget>, Inspector)
        SLATE_ARGUMENT(TSharedPtr<SWidget>, Outliner)
        SLATE_ARGUMENT(TSharedPtr<SWidget>, Assets)
    SLATE_END_ARGS()

    void Construct(const FArguments& Args);
    virtual ~SAuroraViewWorkspace() override;
    void SaveLayout();
private:
    TSharedRef<SDockTab> SpawnPanel(const FSpawnTabArgs& Args);
    TSharedPtr<FTabManager> Manager;
    TSharedPtr<SWidget> Inspector, Outliner, Assets;
};
