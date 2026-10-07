#pragma once

#include "Commandlets/Commandlet.h"
#include "AuroraViewPrepareCommandlet.generated.h"

// Creates the disposable native acceptance map without requiring the optional
// Editor Python plugin, which is not supplied by UE4.18.
UCLASS()
class UAuroraViewPrepareCommandlet final : public UCommandlet
{
    GENERATED_BODY()
public:
    UAuroraViewPrepareCommandlet(const FObjectInitializer& ObjectInitializer);
    virtual int32 Main(const FString& Params) override;
};
