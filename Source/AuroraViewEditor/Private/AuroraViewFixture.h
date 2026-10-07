#pragma once
#include "CoreMinimal.h"
class AActor;

namespace AuroraViewFixture
{
    // Requires disposable project name and explicit command-line mutation flag.
    bool CheckGuard(FString& OutError);
    bool Create(TArray<TWeakObjectPtr<AActor>>& OutActors, FString& OutError);
}
